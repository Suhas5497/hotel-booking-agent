"""Full-conversation evaluations: does a guest actually end up with the right booking?

Single-turn evals only check the first tool call. These journeys play a scripted guest
through a whole conversation against the real model and then check the database:

* the booking / change / cancellation that should exist does exist, with the right details;
* no write ever happened on a turn where the guest had not said yes (hard safety gate).

If Gia asks for something again, the scripted guest repeats the facts (`fallback`); if Gia
shows a preview, the guest says "yes". Each journey gets at most `MAX_TURNS` guest turns.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from goodfoods.agent import is_affirmative

MAX_TURNS = 6
NAME, PHONE, PHONE_N = "Asha Rao", "9876543210", "+919876543210"
SATURDAY, TOMORROW = "2026-10-17", "2026-10-13"   # with the clock frozen at Mon 2026-10-12 16:00


@dataclass
class Journey:
    name: str
    turns: list[str]
    fallback: str
    check: Callable[..., tuple[bool, str]]
    setup: Callable[..., dict] | None = None


def _agent_bookings(service) -> list[dict]:
    return service.db.query("SELECT * FROM reservations WHERE phone = ? AND source = 'agent'", (PHONE_N,))


def _expect_booking(restaurant_id=None, date=None, time=None, party=None, feature=None):
    def check(service, _setup):
        for row in _agent_bookings(service):
            if row["status"] != "confirmed":
                continue
            r = service.resolve_restaurant(row["restaurant_id"])
            if ((restaurant_id is None or row["restaurant_id"] == restaurant_id)
                    and (date is None or row["res_date"] == date) and (time is None or row["res_time"] == time)
                    and (party is None or row["party_size"] == party)
                    and (feature is None or feature in r["features"])):
                return True, f"{row['code']} at {row['restaurant_id']} {row['res_date']} {row['res_time']} x{row['party_size']}"
        return False, f"no matching booking; agent bookings: {[(b['restaurant_id'], b['res_date'], b['res_time'], b['party_size'], b['status']) for b in _agent_bookings(service)]}"
    return check


def _existing_booking(service) -> dict:
    res = service.make_reservation("BLR-016", SATURDAY, "20:00", 2, NAME, PHONE, source="eval-setup")
    return {"code": res["confirmation_code"]}


def _expect_row(**want):
    def check(service, setup):
        rows = service.db.query("SELECT * FROM reservations WHERE code = ?", (setup["code"],))
        if rows and all(rows[0][k] == v for k, v in want.items()):
            return True, f"{setup['code']} {want}"
        return False, f"{setup['code']} is {rows[0] if rows else 'missing'}"
    return check


JOURNEYS = [
    Journey("direct booking",
            ["Book a table at Sakura in HSR Layout this Saturday at 8pm for 2 people",
             f"Name is {NAME}, phone {PHONE}"],
            f"Sakura HSR Layout, Saturday 17 Oct at 8 pm, 2 people, {NAME}, {PHONE}",
            _expect_booking("BLR-016", SATURDAY, "20:00", 2)),
    Journey("recommend then book",
            ["Suggest a rooftop place for a date this Saturday at 8pm, 2 of us",
             f"Book the first one please. {NAME}, {PHONE}"],
            f"The first rooftop option you suggested, Saturday 8 pm, 2 people, {NAME}, {PHONE}",
            _expect_booking(date=SATURDAY, time="20:00", party=2, feature="rooftop")),
    Journey("slot full -> alternative time",
            [f"Book Sakura Indiranagar for Saturday 8pm for 6 people. {NAME}, {PHONE}",
             "OK, 9:30 pm at the same place then"],
            f"Sakura Indiranagar, Saturday 9:30 pm, 6 people, {NAME}, {PHONE}",
            _expect_booking("BLR-003", SATURDAY, "21:30", 6)),
    Journey("hinglish booking",
            [f"Kal raat 8 baje Biryani Co Indiranagar mein 4 logon ke liye table book karo. Naam {NAME}, number {PHONE}"],
            f"Biryani Co Indiranagar, kal raat 8 baje, 4 log, {NAME}, {PHONE}",
            _expect_booking("BLR-001", TOMORROW, "20:00", 4)),
    Journey("modify existing booking",
            ["Please move my booking {code} to 9pm. My phone is " + PHONE],
            "Booking {code}, phone " + PHONE + ", new time 9 pm, same day",
            _expect_row(res_time="21:00", status="confirmed"), _existing_booking),
    Journey("cancel existing booking",
            ["Cancel booking {code}, my number is " + PHONE],
            "Cancel {code}, phone " + PHONE,
            _expect_row(status="cancelled"), _existing_booking),
]


def _write_count(service) -> int:
    return service.db.query("SELECT COUNT(*) n FROM audit_log")[0]["n"]


def run_journey(journey: Journey, agent, service, max_turns: int = MAX_TURNS) -> dict:
    setup = journey.setup(service) if journey.setup else {}
    script = [t.format(**setup) for t in journey.turns]
    transcript, unconfirmed = [], 0
    ok, detail = journey.check(service, setup)
    while not ok and len(transcript) < max_turns:
        if script:
            text = script.pop(0)
        else:
            text = "yes" if agent.pending else journey.fallback.format(**setup)
        before = _write_count(service)
        turn = agent.chat(text)
        if _write_count(service) != before and not is_affirmative(text):
            unconfirmed += 1
        transcript.append({"guest": text, "gia": turn.reply,
                           "tools": [e.name for e in turn.trace if e.kind == "tool"]})
        ok, detail = journey.check(service, setup)
    return {"journey": journey.name, "success": ok, "detail": detail, "turns": len(transcript),
            "unconfirmed_writes": unconfirmed, "transcript": transcript}
