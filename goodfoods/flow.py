"""Step-by-step booking helpers for the UI (no Streamlit here, so they can be unit-tested).

The chat shows different controls at each step (start → options → details → review → booked,
plus change/cancel). Everything the guest picks is turned into a plain message for Gia, so
the model still decides which tool to call; these helpers only make picking easy and honest:
time slots offered are the ones that are actually free.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from . import catalog
from .config import TZ
from .services import (DINING_MINUTES, LAST_SEATING_BEFORE_CLOSE, MIN_LEAD_MINUTES, SLOT_MINUTES,
                       ReservationService, mask_phone)
from .timeparse import human_time


@dataclass(frozen=True)
class Slot:
    hhmm: str          # "20:00"
    label: str         # "8:00 PM"
    free: bool
    meal: str          # "Lunch" | "Dinner" | "Breakfast"


def day_options(today: date, days: int = 7) -> list[tuple[str, str]]:
    """[(iso, label)] for the next `days` days: Today, Tomorrow, then 'Wed 14 Oct'."""
    out = []
    for i in range(days):
        d = today + timedelta(days=i)
        label = "Today" if i == 0 else "Tomorrow" if i == 1 else d.strftime("%a %d %b")
        out.append((d.isoformat(), label))
    return out


def _meal(t: time) -> str:
    return "Breakfast" if t.hour < 11 else "Lunch" if t.hour < 16 else "Dinner"


def available_slots(service: ReservationService, restaurant_id: str, day: date, party: int,
                    current: datetime) -> list[Slot]:
    """Every bookable 30-minute slot for this outlet and day, marked free or full."""
    r = service.resolve_restaurant(restaurant_id)
    open_t, close_t = time.fromisoformat(r["open_time"]), time.fromisoformat(r["close_time"])
    start = open_t.hour * 60 + open_t.minute
    start += (-start) % SLOT_MINUTES                       # first slot on the :00/:30 grid
    last = close_t.hour * 60 + close_t.minute - LAST_SEATING_BEFORE_CLOSE
    earliest = current + timedelta(minutes=MIN_LEAD_MINUTES)
    slots = []
    for m in range(start, last + 1, SLOT_MINUTES):
        t = time(m // 60, m % 60)
        if datetime.combine(day, t, tzinfo=TZ) < earliest:
            continue
        free = service._allocate(r, day, t, party) is not None
        slots.append(Slot(t.strftime("%H:%M"), human_time(t), free, _meal(t)))
    return slots


def booking_request(r: dict, day: date, hhmm: str, party: int, name: str, phone: str,
                    request: str | None = None) -> tuple[str, str]:
    """(message for Gia, short line shown in the chat) for a booking picked in the form."""
    when = f"{day:%A %d %b} ({day.isoformat()}) at {hhmm}"
    text = (f"Please book {r['name']} ({r['id']}) on {when} for {party} guests. "
            f"Name: {name}. Phone: {phone[-10:]}.")
    if request:
        text += f" Special request: {request}."
    shown = (f"{day:%a %d %b} · {human_time(time.fromisoformat(hhmm))} · {party} guests · "
             f"{name} · {mask_phone('+91' + phone[-10:])}")
    return text, shown


def change_request(code: str, phone: str, day: date, hhmm: str, party: int) -> tuple[str, str]:
    text = (f"Please change booking {code} (phone {phone[-10:]}) to {day:%A %d %b} ({day.isoformat()}) "
            f"at {hhmm} for {party} guests.")
    shown = f"Change {code} → {day:%a %d %b} · {human_time(time.fromisoformat(hhmm))} · {party} guests"
    return text, shown


def _ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def calendar_file(code: str, outlet_name: str, address: str, day: str, hhmm: str, party: int,
                  stamp: datetime) -> str:
    """A minimal iCalendar (.ics) event for the booking, 90 minutes long."""
    start = datetime.combine(date.fromisoformat(day), time.fromisoformat(hhmm))
    end = start + timedelta(minutes=DINING_MINUTES)
    fmt = "%Y%m%dT%H%M%S"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//GoodFoods//Gia//EN", "BEGIN:VEVENT",
        f"UID:{code}@goodfoods", f"DTSTAMP:{stamp.astimezone(TZ).strftime(fmt)}",
        f"DTSTART;TZID=Asia/Kolkata:{start.strftime(fmt)}", f"DTEND;TZID=Asia/Kolkata:{end.strftime(fmt)}",
        f"SUMMARY:{_ics_escape('Table at ' + outlet_name)}", f"LOCATION:{_ics_escape(address)}",
        f"DESCRIPTION:{_ics_escape(f'GoodFoods booking {code} for {party} guests')}",
        "END:VEVENT", "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def directions_url(outlet_id: str, area: str) -> str:
    """Google Maps link to the outlet's (demo) location in its neighbourhood."""
    lat, lng = catalog.outlet_coords(outlet_id, area)
    return f"https://www.google.com/maps/search/?api=1&query={lat:.5f},{lng:.5f}"
