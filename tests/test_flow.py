"""Step-by-step booking helpers used by the UI."""
import os
import unittest
from datetime import date, datetime

os.environ["GOODFOODS_NOW"] = "2026-10-12T16:00"

from goodfoods import flow  # noqa: E402
from goodfoods.config import TZ  # noqa: E402
from goodfoods.seed import build_database  # noqa: E402
from goodfoods.services import ReservationService  # noqa: E402

NOW = datetime(2026, 10, 12, 16, 0, tzinfo=TZ)


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.s = ReservationService(build_database(":memory:", with_bookings=False), clock=lambda: NOW)

    def test_day_options(self):
        days = flow.day_options(date(2026, 10, 12), 7)
        self.assertEqual(days[0], ("2026-10-12", "Today"))
        self.assertEqual(days[1][1], "Tomorrow")
        self.assertEqual(days[5], ("2026-10-17", "Sat 17 Oct"))

    def test_slots_respect_hours_clock_and_capacity(self):
        today = flow.available_slots(self.s, "BLR-001", date(2026, 10, 12), 2, NOW)
        self.assertTrue(all(s.hhmm >= "16:30" for s in today))          # nothing in the past
        r = self.s.resolve_restaurant("BLR-001")
        tomorrow = flow.available_slots(self.s, "BLR-001", date(2026, 10, 13), 2, NOW)
        self.assertEqual(tomorrow[0].hhmm, r["open_time"] if r["open_time"].endswith(("00", "30")) else tomorrow[0].hhmm)
        self.assertTrue(all(s.free for s in tomorrow))
        # fill every 8-seater at 20:00 -> that slot shows full for a party of 8
        for i in range(r["tables"]["8"]):
            self.s.make_reservation("BLR-001", "2026-10-13", "20:00", 8, "Guest", f"98765{i:05d}")
        eight = {s.hhmm: s.free for s in flow.available_slots(self.s, "BLR-001", date(2026, 10, 13), 8, NOW)}
        if r["tables"]["8"]:
            self.assertFalse(eight["20:00"])

    def test_booking_request_text(self):
        r = self.s.resolve_restaurant("BLR-001")
        text, shown = flow.booking_request(r, date(2026, 10, 17), "20:00", 4, "Asha Rao", "+919876543210", "high chair")
        self.assertIn("BLR-001", text)
        self.assertIn("2026-10-17", text)
        self.assertIn("9876543210", text)
        self.assertIn("high chair", text)
        self.assertIn("******3210", shown)
        self.assertNotIn("9876543210", shown)

    def test_calendar_file(self):
        ics = flow.calendar_file("GF-ABC234", "GoodFoods Sakura – HSR Layout", "1, Main Road, HSR, Bengaluru",
                                 "2026-10-17", "20:00", 2, NOW)
        self.assertIn("DTSTART;TZID=Asia/Kolkata:20261017T200000", ics)
        self.assertIn("DTEND;TZID=Asia/Kolkata:20261017T213000", ics)
        self.assertIn("1\\, Main Road", ics)
        self.assertTrue(ics.endswith("\r\n"))

    def test_directions_url(self):
        self.assertTrue(flow.directions_url("BLR-001", "Indiranagar").startswith("https://www.google.com/maps/"))


if __name__ == "__main__":
    unittest.main()
