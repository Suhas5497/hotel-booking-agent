"""Deterministic date/time normalisation.

Small models are unreliable at calendar arithmetic ("next Friday" -> wrong date).
We let the model pass whatever the user said and resolve it here, in code, against
the server clock. Ambiguity is surfaced as an error the model can relay, never guessed silently.
"""
from __future__ import annotations

import re
from datetime import date, time, timedelta

WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9,
    "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


class ParseError(ValueError):
    pass


def _with_year(day: int, month: int, year: int | None, today: date) -> date:
    try:
        candidate = date(year or today.year, month, day)
    except ValueError as exc:
        raise ParseError(f"{day}/{month} is not a valid calendar date") from exc
    if year is None and candidate < today:  # "15 Jan" in October means next January
        candidate = date(today.year + 1, month, day)
    return candidate


def parse_date(text: str, today: date) -> date:
    if text is None or not str(text).strip():
        raise ParseError("date is missing")
    s = re.sub(r"[,]", " ", str(text).strip().lower())
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    s = re.sub(r"\s+", " ", s)

    if s in {"today", "tonight", "aaj", "this evening"}:
        return today
    # Hindi "kal" / "parso" mean both yesterday/tomorrow and day-before/day-after.
    # A reservation can only be in the future, so the forward reading is the only valid one;
    # lookups by date never go through this parser.
    if s in {"tomorrow", "tmrw", "tmr", "tomorrow night", "kal", "kal raat", "kal shaam"}:
        return today + timedelta(days=1)
    if s in {"day after tomorrow", "parso", "parson"}:
        return today + timedelta(days=2)
    m = re.fullmatch(r"in (\d{1,2}) days?", s)
    if m:
        return today + timedelta(days=int(m.group(1)))
    try:
        return date.fromisoformat(s)
    except ValueError:
        pass

    m = re.fullmatch(r"(this |next |coming )?([a-z]+)", s)
    if m and m.group(2) in WEEKDAYS:
        delta = (WEEKDAYS[m.group(2)] - today.weekday()) % 7
        if delta == 0 and (m.group(1) or "").strip() == "next":
            delta = 7
        return today + timedelta(days=delta)

    m = re.fullmatch(r"(\d{1,2}) ([a-z]+)(?: (\d{4}))?", s)  # 15 oct [2026]
    if m and m.group(2) in MONTHS:
        return _with_year(int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)) if m.group(3) else None, today)
    m = re.fullmatch(r"([a-z]+) (\d{1,2})(?: (\d{4}))?", s)  # oct 15 [2026]
    if m and m.group(1) in MONTHS:
        return _with_year(int(m.group(2)), MONTHS[m.group(1)], int(m.group(3)) if m.group(3) else None, today)
    m = re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?", s)  # Indian dd/mm[/yyyy]
    if m:
        year = m.group(3)
        if year and len(year) == 2:
            year = "20" + year
        return _with_year(int(m.group(1)), int(m.group(2)), int(year) if year else None, today)

    raise ParseError(f"could not understand the date '{text}'. Use YYYY-MM-DD, 'tomorrow' or a weekday")


def time_candidates(text: str) -> list[time]:
    """All plausible readings of a time, most likely first.

    "19:30", "07:30", "7:30 pm" -> one reading.
    "9" or "9:30" with no AM/PM  -> two readings (21:00 first, then 09:00); the caller
    picks the one the outlet is open for, or asks the guest if both work.
    """
    if text is None or not str(text).strip():
        raise ParseError("time is missing")
    raw = str(text).strip().lower()
    if raw in {"noon", "lunch", "12 noon"}:
        return [time(12, 0)]
    for word, reading in (("morning", "am"), ("breakfast", "am"), ("night", "pm"), ("evening", "pm"),
                          ("dinner", "pm"), ("raat", "pm"), ("shaam", "pm"), ("subah", "am")):
        if word in raw:
            raw = re.sub(r"[a-z]+", " ", raw).strip() + reading
            break
    # "8 baje" (Hindi "o'clock") and "8 o'clock" say nothing about AM/PM: drop the word and let
    # the caller resolve the reading against opening hours.
    raw = re.sub(r"\b(baje|bje|o'?clock|oclock)\b", " ", raw)
    s = raw.replace(" ", "").replace(".", "").replace("hrs", "").replace("hours", "")
    m = re.fullmatch(r"(\d{1,2}):?(\d{2})?(?::\d{2})?(am|pm)?", s)
    if not m:
        raise ParseError(f"could not understand the time '{text}'. Use 24h HH:MM, e.g. 19:30")
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if minute > 59:
        raise ParseError(f"invalid minutes in '{text}'")
    if ampm:
        if not 1 <= hour <= 12:
            raise ParseError(f"invalid hour in '{text}'")
        return [time(hour % 12 + (12 if ampm == "pm" else 0), minute)]
    if hour > 23:
        raise ParseError(f"invalid hour in '{text}'")
    # "09:30", "10:00", "19:30" are written as 24h clock times; "9", "9:30", "10" are not.
    explicit_24h = (m.group(1).startswith("0") or hour == 0 or hour >= 12
                    or (len(m.group(1)) == 2 and m.group(2) is not None))
    if explicit_24h:
        return [time(hour, minute)]
    pm, am = time(hour + 12, minute), time(hour, minute)
    # 11 without AM/PM is usually late morning; 1-10 is usually evening in a restaurant.
    return [am, pm] if hour == 11 else [pm, am]


def parse_time(text: str) -> time:
    """Single most likely reading (see time_candidates for the ambiguity-aware version)."""
    return time_candidates(text)[0]


def snap_to_slot(t: time, slot_minutes: int = 30) -> tuple[time, bool]:
    """Round to the nearest bookable slot. Returns (slot, was_adjusted)."""
    total = t.hour * 60 + t.minute
    snapped = int(round(total / slot_minutes) * slot_minutes)
    snapped = min(snapped, 23 * 60 + 30)
    out = time(snapped // 60, snapped % 60)
    return out, out != t


def fmt_time(t: time) -> str:
    return t.strftime("%H:%M")


def human_time(t: time) -> str:
    return t.strftime("%I:%M %p").lstrip("0")


def human_date(d: date) -> str:
    return d.strftime("%a, %d %b %Y")
