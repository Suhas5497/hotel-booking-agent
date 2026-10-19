"""Business logic for GoodFoods reservations.

This layer is LLM-agnostic: every rule (hours, capacity, verification, double-booking)
is enforced here in deterministic code. The LLM decides *what* to do; this code decides
*whether it is allowed*. Errors come back as structured `ToolError`s with a hint the
model can act on (self-correction instead of hallucination).
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import date, datetime, time, timedelta
from typing import Callable

from . import catalog
from .config import TZ, now as default_now
from .db import Database
from .timeparse import ParseError, fmt_time, human_date, human_time, parse_date, snap_to_slot, time_candidates

DINING_MINUTES = 90          # a table is held for 90 minutes
SLOT_MINUTES = 30
LAST_SEATING_BEFORE_CLOSE = 60
MAX_PARTY = 20
MAX_TABLE = 8
MAX_ADVANCE_DAYS = 60
MIN_LEAD_MINUTES = 15
TABLE_SIZES = (2, 4, 6, 8)
PRIVATE_ROOM = -1
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O/1/I/L confusion over the phone
CODE_RE = re.compile(r"\bGF-[A-Z0-9]{6}\b")
MAX_FAILED_VERIFICATIONS = 5     # wrong code+phone attempts per phone ...
LOCKOUT_MINUTES = 15             # ... within this window before lookups are paused


class ToolError(Exception):
    def __init__(self, code: str, message: str, hint: str | None = None, **extra):
        super().__init__(message)
        self.code, self.message, self.hint, self.extra = code, message, hint, extra

    def to_dict(self) -> dict:
        out = {"error": self.code, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        out.update(self.extra)
        return out


# ----------------------------------------------------------------------------- helpers

def normalize_phone(phone) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if not re.fullmatch(r"[6-9]\d{9}", digits):
        raise ToolError("INVALID_PHONE", "Phone number must be a valid 10-digit Indian mobile number.",
                        hint="Ask the guest for their 10-digit mobile number.")
    return "+91" + digits


def mask_phone(phone: str) -> str:
    return f"+91 ******{phone[-4:]}"


def mask_code(code: str) -> str:
    """'GF-ABC123' -> 'GF-****23'. Deliberately does not match CODE_RE, so it can never be
    passed back as a working code (or slip past the hallucination guard)."""
    return f"GF-****{code[-2:]}"


def validate_name(name) -> str:
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    if not (2 <= len(name) <= 60) or not re.fullmatch(r"[A-Za-zऀ-ॿ .'\-]+", name):
        raise ToolError("INVALID_NAME", "A guest name (2-60 letters) is required for the booking.",
                        hint="Ask the guest for the name to hold the table under.")
    return name


def validate_party(party_size) -> int:
    try:
        n = int(party_size)
    except (TypeError, ValueError):
        raise ToolError("INVALID_PARTY_SIZE", "party_size must be a whole number.",
                        hint="Ask how many people are coming.") from None
    if n < 1:
        raise ToolError("INVALID_PARTY_SIZE", "party_size must be at least 1.")
    if n > MAX_PARTY:
        raise ToolError("PARTY_TOO_LARGE", f"Online bookings are limited to {MAX_PARTY} guests.",
                        hint="Offer the events team (events@goodfoods.in) for large groups and buyouts.")
    return n


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _t(minutes: int) -> time:
    return time(minutes // 60, minutes % 60)


_NON_VEG_RE = re.compile(r"\bnon[\s\-]*(veg|vegetarian)\b", re.I)
_VEG_RE = re.compile(r"\b(veg|vegetarian|vegan|pure[\s\-]*veg|shakahari)\b", re.I)


def wants_veg_only(preferences) -> bool:
    """True only for an actual vegetarian request. 'non-veg' / 'non-vegetarian' contain the
    word 'veg' but mean the opposite, so they are removed before matching."""
    text = _NON_VEG_RE.sub(" ", str(preferences or ""))
    return bool(_VEG_RE.search(text))


_HIGHLIGHT_ORDER = ["rooftop", "private_dining", "live_music", "pure_veg", "outdoor_seating", "quiet_zone", "bar",
                    "kid_friendly", "pet_friendly", "wheelchair_accessible", "valet_parking", "wifi"]


def _brief(r: dict) -> dict:
    return {
        "restaurant_id": r["id"],
        "name": r["name"],
        "area": r["area"],
        "cuisines": r["cuisines"],
        "price_per_person_inr": r["price_per_person"],
        "rating": r["rating"],
        "highlights": sorted(r["features"], key=_HIGHLIGHT_ORDER.index)[:4],
    }


class ReservationService:
    def __init__(self, db: Database, clock: Callable[[], datetime] = default_now):
        self.db = db
        self.clock = clock
        self._failed: dict[str, list[datetime]] = {}   # phone -> recent failed code checks

    # ------------------------------------------------------------------ lookups
    def _all_restaurants(self) -> list[dict]:
        return self.db.query("SELECT * FROM restaurants ORDER BY id")

    def resolve_restaurant(self, ref) -> dict:
        """Accepts an id ('BLR-012') or a (partial) name - small models often pass names."""
        if not ref or not str(ref).strip():
            raise ToolError("MISSING_RESTAURANT", "restaurant_id is required.",
                            hint="Search or recommend first, then pass the restaurant_id.")
        s = str(ref).strip()
        rows = self.db.query("SELECT * FROM restaurants WHERE upper(id) = ?", (s.upper(),))
        if rows:
            return rows[0]
        needle = s.lower().replace("goodfoods", "").replace("–", " ").replace("-", " ").strip()
        tokens = [t for t in re.split(r"\s+", needle) if t]
        matches = [r for r in self._all_restaurants()
                   if tokens and all(t in r["name"].lower().replace("–", " ") for t in tokens)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ToolError("AMBIGUOUS_RESTAURANT", f"'{s}' matches {len(matches)} outlets.",
                            hint="Ask the guest which area they mean.",
                            candidates=[{"restaurant_id": m["id"], "name": m["name"]} for m in matches[:6]])
        raise ToolError("RESTAURANT_NOT_FOUND", f"No GoodFoods outlet matches '{s}'.",
                        hint="Use search_restaurants to find a valid restaurant_id.")

    # ------------------------------------------------------------------ time rules
    def _parse_when(self, r: dict, date_s, time_s) -> tuple[date, time, list[str]]:
        """Resolve date + time for one outlet. A time without AM/PM ("9") is resolved
        against this outlet's hours; if both readings are bookable we ask instead of guessing."""
        try:
            candidates = time_candidates(time_s)
        except ParseError as exc:
            raise ToolError("INVALID_DATETIME", str(exc), hint="Confirm the time with the guest.") from None
        if len(candidates) == 1:
            return self._parse_when_exact(r, date_s, candidates[0])
        valid, first_error = [], None
        for cand in candidates:
            try:
                valid.append(self._parse_when_exact(r, date_s, cand))
            except ToolError as exc:
                if exc.code == "INVALID_DATETIME":
                    raise
                first_error = first_error or exc
        if len(valid) == 1:
            d, t, notes = valid[0]
            return d, t, notes + [f"Read '{time_s}' as {human_time(t)} (the only time {r['name']} is open)."]
        if not valid:
            raise first_error
        a, b = (human_time(v[1]) for v in valid)
        raise ToolError("AMBIGUOUS_TIME", f"'{time_s}' could mean {a} or {b}; {r['name']} is open at both.",
                        hint="Ask the guest whether they mean morning or evening.", options=[a, b])

    def _parse_when_exact(self, r: dict, date_s, t: time) -> tuple[date, time, list[str]]:
        current = self.clock()
        notes: list[str] = []
        try:
            d = parse_date(date_s, current.date())
        except ParseError as exc:
            raise ToolError("INVALID_DATETIME", str(exc), hint="Confirm the date with the guest.") from None
        t2, adjusted = snap_to_slot(t, SLOT_MINUTES)
        if adjusted:
            notes.append(f"Requested {human_time(t)} is not a booking slot; using the nearest slot, "
                         f"{human_time(t2)}. Tell the guest.")
        t = t2
        when = datetime.combine(d, t, tzinfo=TZ)
        if when < current + timedelta(minutes=MIN_LEAD_MINUTES):
            raise ToolError("TIME_IN_PAST", f"{human_date(d)} {human_time(t)} has already passed or is too soon.",
                            hint=f"Current time is {current:%a %d %b %H:%M}. Ask for a later time.")
        if d > current.date() + timedelta(days=MAX_ADVANCE_DAYS):
            raise ToolError("TOO_FAR_AHEAD", f"Bookings open only {MAX_ADVANCE_DAYS} days in advance.")
        open_t, close_t = time.fromisoformat(r["open_time"]), time.fromisoformat(r["close_time"])
        last = _minutes(close_t) - LAST_SEATING_BEFORE_CLOSE
        if not (_minutes(open_t) <= _minutes(t) <= last):
            raise ToolError("OUTSIDE_HOURS",
                            f"{r['name']} seats guests between {human_time(open_t)} and {human_time(_t(last))}.",
                            hint="Offer a time inside these hours.",
                            opening_hours=f"{r['open_time']}-{r['close_time']}")
        return d, t, notes

    # ------------------------------------------------------------------ capacity
    def _occupancy(self, rid: str, d: date, t: time, exclude_code: str | None = None) -> dict[int, int]:
        """Peak number of tables of each size in use at any moment of the new sitting [t, t+90).

        Counting every overlapping booking over-states demand: a 6:30 and an 8:00 sitting don't
        overlap each other, so one table serves both, yet both overlap a 7:30 request. What
        matters is the most tables busy *at the same time*. (Sittings are intervals, so if the
        peak fits, a table assignment always exists.)"""
        rows = self.db.query(
            "SELECT code, res_time, table_size FROM reservations "
            "WHERE restaurant_id = ? AND res_date = ? AND status = 'confirmed'", (rid, d.isoformat()))
        start = _minutes(t)
        end = start + DINING_MINUTES
        starts: dict[int, list[int]] = {}
        for row in rows:
            if exclude_code and row["code"] == exclude_code:
                continue
            other = _minutes(time.fromisoformat(row["res_time"]))
            if other < end and start < other + DINING_MINUTES:   # overlaps the new sitting
                starts.setdefault(row["table_size"], []).append(other)
        used: dict[int, int] = {}
        for size, sizes_starts in starts.items():
            # Concurrency only rises when a sitting begins, so check the new start and every
            # booking start that falls inside the new sitting.
            points = [start] + [s for s in sizes_starts if start <= s < end]
            used[size] = max(sum(1 for s in sizes_starts if s <= p < s + DINING_MINUTES) for p in points)
        return used

    def _allocate(self, r: dict, d: date, t: time, party: int, exclude_code: str | None = None) -> int | None:
        used = self._occupancy(r["id"], d, t, exclude_code)
        if party <= MAX_TABLE:
            for size in TABLE_SIZES:   # smallest table that fits -> best yield
                if size >= party and used.get(size, 0) < r["tables"].get(str(size), 0):
                    return size
            return None
        if r["private_dining_capacity"] >= party and used.get(PRIVATE_ROOM, 0) == 0:
            return PRIVATE_ROOM
        return None

    def _alternatives(self, r: dict, d: date, t: time, party: int, exclude_code=None, n: int = 4) -> list[str]:
        open_m = _minutes(time.fromisoformat(r["open_time"]))
        last_m = _minutes(time.fromisoformat(r["close_time"])) - LAST_SEATING_BEFORE_CLOSE
        earliest = self.clock() + timedelta(minutes=MIN_LEAD_MINUTES)
        found = []
        for delta in range(SLOT_MINUTES, 181, SLOT_MINUTES):   # within +-3h, closest first
            for m in (_minutes(t) - delta, _minutes(t) + delta):
                if not (open_m <= m <= last_m):
                    continue
                cand = _t(m)
                if datetime.combine(d, cand, tzinfo=TZ) < earliest:
                    continue
                if self._allocate(r, d, cand, party, exclude_code) is not None:
                    found.append(fmt_time(cand))
            if len(found) >= n:
                break
        return sorted(found[:n])

    @staticmethod
    def _seating_label(size: int) -> str:
        return "private dining room" if size == PRIVATE_ROOM else f"{size}-seat table"

    # ------------------------------------------------------------------ scoring
    def _score(self, r: dict, *, cuisine_terms: set[str], area: str | None, budget: int | None,
               wanted_features: list[str], keywords: list[str]) -> tuple[float, list[str]]:
        reasons = []
        hay = " ".join(r["cuisines"] + r["signature_dishes"] + [r["concept"], r["description"]]).lower()
        # cuisine (0.30)
        if cuisine_terms:
            hit = any(term in c.lower() for term in cuisine_terms for c in r["cuisines"]) or \
                any(term in hay for term in cuisine_terms)
            cuisine_s = 1.0 if hit else 0.0
            if hit:
                reasons.append(f"serves {', '.join(r['cuisines'])}")
        else:
            cuisine_s = 0.5
        # area (0.15)
        if area:
            if r["area"] == area:
                area_s = 1.0
                reasons.append(f"in {area}")
            elif r["area"] in catalog.NEARBY.get(area, []):
                area_s = 0.5
                reasons.append(f"in {r['area']}, next to {area}")
            else:
                area_s = 0.0
        else:
            area_s = 0.5
        # budget (0.15)
        if budget:
            price = r["price_per_person"]
            budget_s = 1.0 if price <= budget else max(0.0, 1 - (price - budget) / budget)
            if price <= budget:
                reasons.append(f"~₹{price}/person fits your ₹{budget} budget")
        else:
            budget_s = 0.5
        # occasion / features (0.15)
        if wanted_features:
            matched = [f for f in wanted_features if f in r["features"]]
            feat_s = len(matched) / len(wanted_features)
            if matched:
                reasons.append("has " + ", ".join(m.replace("_", " ") for m in matched))
        else:
            feat_s = 0.5
        # free-text preferences (0.05)
        kw_s = 0.5
        if keywords:
            hits = [k for k in keywords if k in hay]
            kw_s = len(hits) / len(keywords)
            if hits:
                reasons.append("known for " + ", ".join(hits))
        # quality (0.20)
        rating_s = min(1.0, max(0.0, (r["rating"] - 3.5) / 1.5))
        if r["rating"] >= 4.5:
            reasons.append(f"top-rated ({r['rating']}★)")
        score = 0.30 * cuisine_s + 0.15 * area_s + 0.15 * budget_s + 0.15 * feat_s + 0.05 * kw_s + 0.20 * rating_s
        return round(score, 3), reasons

    def _availability_note(self, r, date_s, time_s, party) -> dict:
        """Per-outlet availability annotation; outlet-specific rule violations (e.g. hours)
        become a reason instead of failing the whole search."""
        try:
            d, t, _ = self._parse_when(r, date_s, time_s)
        except ToolError as exc:
            if exc.code not in ("OUTSIDE_HOURS", "AMBIGUOUS_TIME"):
                raise
            return {"available_at_requested_time": False, "reason": exc.message}
        if self._allocate(r, d, t, party) is not None:
            return {"available_at_requested_time": True}
        return {"available_at_requested_time": False, "other_times": self._alternatives(r, d, t, party, n=3)}

    # ================================================================== TOOLS
    def search_restaurants(self, cuisine=None, area=None, party_size=None, date=None, time=None,
                           max_price_per_person=None, features=None, veg_only=None, limit=5) -> dict:
        if time and not date:   # "7pm" with no date means today — and must be checked against the clock
            date = "today"
        area_n = catalog.normalize_area(area)
        if area and not area_n:
            raise ToolError("UNKNOWN_AREA", f"GoodFoods has no outlet in '{area}'.",
                            hint="Offer one of the served areas.", served_areas=catalog.AREAS)
        terms = catalog.expand_cuisine(cuisine)
        feats = catalog.normalize_features(features)
        if veg_only and "pure_veg" not in feats:
            feats.append("pure_veg")
        party = validate_party(party_size) if party_size else None
        try:
            limit = max(1, min(int(limit or 5), 10))
        except (TypeError, ValueError):
            limit = 5

        results = []
        for r in self._all_restaurants():
            if area_n and r["area"] != area_n:
                continue
            if terms and not any(term in c.lower() for term in terms for c in r["cuisines"]):
                continue
            if max_price_per_person and r["price_per_person"] > int(max_price_per_person):
                continue
            if any(f not in r["features"] for f in feats):
                continue
            if party and party > MAX_TABLE and r["private_dining_capacity"] < party:
                continue
            results.append(r)

        results.sort(key=lambda r: (-r["rating"], r["price_per_person"]))
        widened = None
        if not results and area_n:   # graceful widening to neighbouring areas
            near = self.search_restaurants(cuisine, None, party_size, None, None,
                                           max_price_per_person, features, veg_only, 50)["restaurants"]
            ids = {x["restaurant_id"] for x in near}
            results = [r for r in self._all_restaurants()
                       if r["id"] in ids and r["area"] in catalog.NEARBY.get(area_n, [])]
            results.sort(key=lambda r: (-r["rating"], r["price_per_person"]))
            widened = f"No match in {area_n}; showing nearby areas."

        out_rows = []
        for r in results[:limit]:
            row = _brief(r)
            if date and time:
                row.update(self._availability_note(r, date, time, party or 2))
            out_rows.append(row)
        out = {"count": len(results), "restaurants": out_rows}
        if widened:
            out["note"] = widened
        if not results:
            out["hint"] = "No exact match. Relax a filter (cuisine, budget or features) or try recommend_restaurants."
        return out

    def recommend_restaurants(self, occasion=None, cuisine=None, area=None, party_size=2, date=None,
                              time=None, budget_per_person=None, preferences=None, limit=3) -> dict:
        if time and not date:   # "7pm" with no date means today — and must be checked against the clock
            date = "today"
        area_n = catalog.normalize_area(area)
        if area and not area_n:
            raise ToolError("UNKNOWN_AREA", f"GoodFoods has no outlet in '{area}'.",
                            hint="Offer one of the served areas.", served_areas=catalog.AREAS)
        party = validate_party(party_size or 2)
        occ = catalog.normalize_occasion(occasion) or catalog.normalize_occasion(preferences)
        wanted = list(catalog.OCCASIONS.get(occ, [])) if occ else []
        named = catalog.normalize_features([w for w in re.split(r"[,\s]+", str(preferences or "")) if w])
        wanted += [f for f in named if f not in wanted]
        keywords = [w for w in re.split(r"[^a-z]+", str(preferences or "").lower()) if len(w) > 3][:6]
        veg = wants_veg_only(preferences)
        budget = int(budget_per_person) if budget_per_person else None
        terms = catalog.expand_cuisine(cuisine)
        try:
            limit = max(1, min(int(limit or 3), 5))
        except (TypeError, ValueError):
            limit = 3

        scored = []
        for r in self._all_restaurants():
            if veg and not r["veg_only"]:
                continue
            if party > MAX_TABLE and r["private_dining_capacity"] < party:
                continue
            s, why = self._score(r, cuisine_terms=terms, area=area_n, budget=budget,
                                 wanted_features=wanted, keywords=keywords)
            scored.append((s, r, why))
        scored.sort(key=lambda x: (-x[0], -x[1]["rating"]))
        # Features the guest named ("rooftop", "outdoor") are requirements, not nice-to-haves —
        # unless no outlet has them all, in which case we fall back to the best partial matches.
        must_have = [x for x in scored if all(f in x[1]["features"] for f in named)]
        if named and must_have:
            scored = must_have

        rows = [_brief(r) | {"match_score": s, "why": why[:3] or ["well rated all-rounder"]}
                for s, r, why in scored[:15]]
        if date and time:
            by_id = {r["id"]: r for _, r, _ in scored[:15]}
            for row in rows:
                row.update(self._availability_note(by_id[row["restaurant_id"]], date, time, party))
            # A great match you can't sit at is a worse answer than a good match you can.
            free = [r for r in rows if r["available_at_requested_time"]]
            flexible = [r for r in rows if not r["available_at_requested_time"] and r.get("other_times")]
            rows = free + flexible
        # Diversity re-rank: three Skyline Grills is one option, not three.
        concept_of = {r["id"]: r["concept"] for _, r, _ in scored[:15]}
        picks, seen = [], set()
        for row in rows:
            if concept_of[row["restaurant_id"]] not in seen:
                picks.append(row)
                seen.add(concept_of[row["restaurant_id"]])
        picks += [row for row in rows if row not in picks]
        return {"occasion_detected": occ, "recommendations": picks[:limit]}

    def get_restaurant_details(self, restaurant_id) -> dict:
        r = self.resolve_restaurant(restaurant_id)
        return {
            **_brief(r),
            "address": r["address"],
            "phone": r["phone"],
            "opening_hours": f"{r['open_time']}-{r['close_time']} (last seating "
                             f"{fmt_time(_t(_minutes(time.fromisoformat(r['close_time'])) - LAST_SEATING_BEFORE_CLOSE))})",
            "total_seats": r["total_seats"],
            "largest_table": max(int(k) for k, v in r["tables"].items() if v),
            "private_dining_capacity": r["private_dining_capacity"] or None,
            "features": r["features"],
            "signature_dishes": r["signature_dishes"],
            "pure_veg": r["veg_only"],
            "description": r["description"],
        }

    def check_availability(self, restaurant_id, date, time, party_size) -> dict:
        r = self.resolve_restaurant(restaurant_id)
        party = validate_party(party_size)
        d, t, notes = self._parse_when(r, date, time)
        size = self._allocate(r, d, t, party)
        out = {"restaurant_id": r["id"], "name": r["name"], "date": d.isoformat(),
               "day": human_date(d), "time": fmt_time(t), "party_size": party, "available": size is not None}
        if notes:
            out["notes"] = notes
        if size is not None:
            out["seating"] = self._seating_label(size)
        else:
            out["alternative_times_same_day"] = self._alternatives(r, d, t, party)
            out["similar_outlets_free_at_this_time"] = self._similar_free(r, d, t, party)
            if party > MAX_TABLE and not r["private_dining_capacity"]:
                out["reason"] = f"This outlet's largest table seats {MAX_TABLE} and it has no private dining room."
        return out

    def _similar_free(self, r, d, t, party, n=2) -> list[dict]:
        out = []
        cuisines = {c.lower() for c in r["cuisines"]}
        for other in self._all_restaurants():
            if other["id"] == r["id"]:
                continue
            same_food = bool(cuisines & {c.lower() for c in other["cuisines"]})
            close = other["area"] == r["area"] or other["area"] in catalog.NEARBY.get(r["area"], [])
            if not (same_food and close):
                continue
            try:
                self._parse_when(other, d.isoformat(), fmt_time(t))
            except ToolError:
                continue
            if self._allocate(other, d, t, party) is not None:
                out.append({"restaurant_id": other["id"], "name": other["name"], "area": other["area"]})
            if len(out) >= n:
                break
        return out

    def make_reservation(self, restaurant_id, date, time, party_size, customer_name, phone,
                         special_requests=None, dry_run=False, source="agent") -> dict:
        r = self.resolve_restaurant(restaurant_id)
        party = validate_party(party_size)
        name = validate_name(customer_name)
        ph = normalize_phone(phone)
        req = (str(special_requests).strip()[:200] or None) if special_requests else None
        d, t, notes = self._parse_when(r, date, time)

        with self.db.transaction() as conn:
            for row in conn.execute(
                    "SELECT code, restaurant_id, res_time FROM reservations WHERE phone = ? AND res_date = ? "
                    "AND status = 'confirmed'", (ph, d.isoformat())).fetchall():
                if abs(_minutes(datetime.strptime(row["res_time"], "%H:%M").time()) - _minutes(t)) < DINING_MINUTES:
                    raise ToolError("DUPLICATE_BOOKING",
                                    f"This phone already has booking {row['code']} at {row['res_time']} that day.",
                                    hint="Ask whether to modify the existing booking instead.",
                                    existing_code=row["code"])
            size = self._allocate(r, d, t, party)
            if size is None:
                raise ToolError("NOT_AVAILABLE", f"{r['name']} is full for {party} at {human_time(t)} on {human_date(d)}.",
                                hint="Offer the alternatives.",
                                alternative_times_same_day=self._alternatives(r, d, t, party),
                                similar_outlets_free_at_this_time=self._similar_free(r, d, t, party))
            summary = {"restaurant_id": r["id"], "restaurant": r["name"], "address": r["address"],
                       "date": d.isoformat(), "day": human_date(d), "time": fmt_time(t),
                       "party_size": party, "name": name, "phone": mask_phone(ph),
                       "seating": self._seating_label(size), "special_requests": req}
            if dry_run:
                return {"status": "ready_to_book", "summary": summary, **({"notes": notes} if notes else {})}
            code = self._new_code(conn)
            ts = self.clock().isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO reservations (code, restaurant_id, customer_name, phone, party_size, res_date, res_time,"
                " table_size, status, special_requests, source, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?, 'confirmed', ?,?,?,?)",
                (code, r["id"], name, ph, party, d.isoformat(), fmt_time(t), size, req, source, ts, ts))
            self._audit(conn, "create", code, summary)
        return {"status": "confirmed", "confirmation_code": code, "summary": summary,
                **({"notes": notes} if notes else {})}

    def _new_code(self, conn) -> str:
        while True:
            code = "GF-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
            if not conn.execute("SELECT 1 FROM reservations WHERE code = ?", (code,)).fetchone():
                return code

    def _audit(self, conn, action: str, code: str | None, detail: dict) -> None:
        conn.execute("INSERT INTO audit_log (ts, action, code, detail) VALUES (?,?,?,?)",
                     (self.clock().isoformat(timespec="seconds"), action, code, json.dumps(detail, default=str)))

    def _verified(self, code, phone) -> dict:
        """Code + phone together prove a booking is the guest's. Both are required: the phone
        alone is not a secret (anyone can know your number), the code is."""
        code_n = str(code or "").strip().upper().replace(" ", "")
        if code_n and not code_n.startswith("GF-") and len(code_n) == 8 and code_n.startswith("GF"):
            code_n = "GF-" + code_n[2:]
        if not CODE_RE.fullmatch(code_n):
            raise ToolError("INVALID_CODE", "Confirmation codes look like GF-XXXXXX.",
                            hint="Ask the guest for the full code from their confirmation message.")
        ph = normalize_phone(phone)
        current = self.clock()
        window = timedelta(minutes=LOCKOUT_MINUTES)
        recent = [t for t in self._failed.get(ph, []) if current - t < window]
        self._failed[ph] = recent
        if len(recent) >= MAX_FAILED_VERIFICATIONS:
            raise ToolError("TOO_MANY_ATTEMPTS", "Too many incorrect codes for this number. Please try again later.",
                            hint=f"Tell the guest to wait {LOCKOUT_MINUTES} minutes or call the outlet directly.")
        rows = self.db.query(
            "SELECT res.*, r.name AS restaurant, r.address FROM reservations res "
            "JOIN restaurants r ON r.id = res.restaurant_id WHERE res.code = ?", (code_n,))
        # Same message for "no such code" and "wrong phone": don't leak which bookings exist.
        if not rows or rows[0]["phone"] != ph:
            recent.append(current)
            raise ToolError("BOOKING_NOT_FOUND", "No booking matches that code and phone number.",
                            hint="Double-check the code and the phone number used when booking.")
        self._failed.pop(ph, None)
        return rows[0]

    @staticmethod
    def _public(res: dict, reveal_code: bool = True) -> dict:
        d = date.fromisoformat(res["res_date"])
        return {"confirmation_code": res["code"] if reveal_code else mask_code(res["code"]), "status": res["status"], "restaurant_id": res["restaurant_id"],
                "restaurant": res["restaurant"], "date": res["res_date"], "day": human_date(d),
                "time": res["res_time"], "party_size": res["party_size"], "name": res["customer_name"],
                "phone": mask_phone(res["phone"]), "special_requests": res["special_requests"]}

    def lookup_reservation(self, phone, confirmation_code=None) -> dict:
        if confirmation_code:
            return {"reservations": [self._public(self._verified(confirmation_code, phone))]}
        ph = normalize_phone(phone)
        rows = self.db.query(
            "SELECT res.*, r.name AS restaurant, r.address FROM reservations res "
            "JOIN restaurants r ON r.id = res.restaurant_id WHERE res.phone = ? AND res.status = 'confirmed' "
            "AND res.res_date >= ? ORDER BY res.res_date, res.res_time LIMIT 5", (ph, self.clock().date().isoformat()))
        if not rows:
            return {"reservations": [], "hint": "No upcoming bookings for this number."}
        # Knowing a phone number is not proof of owning the booking, so codes stay masked here.
        # Changing or cancelling needs the full code from the guest's confirmation.
        return {"reservations": [self._public(r, reveal_code=False) for r in rows],
                "codes_hidden": True,
                "hint": "Codes are hidden. To change or cancel, ask the guest for the full code from their "
                        "confirmation, then call lookup_reservation again with phone and confirmation_code."}

    def modify_reservation(self, confirmation_code, phone, new_date=None, new_time=None,
                           new_party_size=None, special_requests=None, dry_run=False) -> dict:
        res = self._verified(confirmation_code, phone)
        if res["status"] != "confirmed":
            raise ToolError("NOT_MODIFIABLE", f"Booking {res['code']} is {res['status']}.")
        if not any([new_date, new_time, new_party_size, special_requests]):
            raise ToolError("NOTHING_TO_CHANGE", "No change was specified.",
                            hint="Ask what the guest wants to change: date, time, party size or requests.")
        r = self.resolve_restaurant(res["restaurant_id"])
        d, t, notes = self._parse_when(r, new_date or res["res_date"], new_time or res["res_time"])
        party = validate_party(new_party_size or res["party_size"])
        req = (str(special_requests).strip()[:200] if special_requests else res["special_requests"])
        with self.db.transaction() as conn:
            size = self._allocate(r, d, t, party, exclude_code=res["code"])
            if size is None:
                raise ToolError("NOT_AVAILABLE", f"Cannot move booking to {human_time(t)} on {human_date(d)} for {party}.",
                                hint="Offer the alternatives; the original booking is unchanged.",
                                alternative_times_same_day=self._alternatives(r, d, t, party, exclude_code=res["code"]))
            before = self._public(res)
            after = before | {"date": d.isoformat(), "day": human_date(d), "time": fmt_time(t),
                              "party_size": party, "special_requests": req}
            if dry_run:
                return {"status": "ready_to_modify", "before": before, "after": after,
                        **({"notes": notes} if notes else {})}
            conn.execute("UPDATE reservations SET res_date=?, res_time=?, party_size=?, table_size=?, "
                         "special_requests=?, updated_at=? WHERE code=?",
                         (d.isoformat(), fmt_time(t), party, size, req,
                          self.clock().isoformat(timespec="seconds"), res["code"]))
            self._audit(conn, "modify", res["code"], {"before": before, "after": after})
        return {"status": "modified", "reservation": after}

    def cancel_reservation(self, confirmation_code, phone, dry_run=False) -> dict:
        res = self._verified(confirmation_code, phone)
        if res["status"] != "confirmed":
            raise ToolError("ALREADY_CANCELLED", f"Booking {res['code']} is already {res['status']}.")
        booking = self._public(res)
        if dry_run:
            return {"status": "ready_to_cancel", "reservation": booking}
        with self.db.transaction() as conn:
            conn.execute("UPDATE reservations SET status='cancelled', updated_at=? WHERE code=?",
                         (self.clock().isoformat(timespec="seconds"), res["code"]))
            self._audit(conn, "cancel", res["code"], booking)
        return {"status": "cancelled", "reservation": booking | {"status": "cancelled"}}

    # ------------------------------------------------------------------ ops helpers (UI only)
    def upcoming_reservations(self, limit: int = 50) -> list[dict]:
        return self.db.query(
            "SELECT res.code, r.name AS restaurant, res.res_date, res.res_time, res.party_size, res.customer_name, "
            "res.status, res.source, res.created_at FROM reservations res JOIN restaurants r ON r.id = res.restaurant_id "
            "WHERE res.source != 'seed' ORDER BY res.created_at DESC LIMIT ?", (limit,))
