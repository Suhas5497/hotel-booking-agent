"""Unit tests: date parsing, business rules, MCP protocol, LLM output salvage.
Run:  python -m unittest discover -s tests -v      (also works under pytest)"""
import json
import os
import unittest
from datetime import date, datetime, time

os.environ["GOODFOODS_NOW"] = "2026-10-12T16:00"   # Monday

from goodfoods.config import TZ  # noqa: E402
from goodfoods.db import Database  # noqa: E402
from goodfoods.llm import parse_text_tool_calls  # noqa: E402
from goodfoods.mcp_client import InProcessTransport, MCPClient  # noqa: E402
from goodfoods.mcp_server import MCPServer  # noqa: E402
from goodfoods.seed import build_database, generate_restaurants  # noqa: E402
from goodfoods.services import ReservationService, ToolError, normalize_phone  # noqa: E402
from goodfoods.timeparse import ParseError, parse_date, parse_time, time_candidates  # noqa: E402

TODAY = date(2026, 10, 12)


def fresh_service(with_bookings=False) -> ReservationService:
    db = build_database(":memory:", with_bookings=with_bookings)
    return ReservationService(db, clock=lambda: datetime(2026, 10, 12, 16, 0, tzinfo=TZ))


class TimeParsingTests(unittest.TestCase):
    def test_relative_dates(self):
        self.assertEqual(parse_date("tomorrow", TODAY), date(2026, 10, 13))
        self.assertEqual(parse_date("Friday", TODAY), date(2026, 10, 16))
        self.assertEqual(parse_date("next monday", TODAY), date(2026, 10, 19))
        self.assertEqual(parse_date("monday", TODAY), TODAY)
        self.assertEqual(parse_date("in 3 days", TODAY), date(2026, 10, 15))

    def test_absolute_dates(self):
        self.assertEqual(parse_date("2026-10-20", TODAY), date(2026, 10, 20))
        self.assertEqual(parse_date("20th Oct", TODAY), date(2026, 10, 20))
        self.assertEqual(parse_date("Oct 20", TODAY), date(2026, 10, 20))
        self.assertEqual(parse_date("20/10", TODAY), date(2026, 10, 20))      # Indian dd/mm
        self.assertEqual(parse_date("5 jan", TODAY), date(2027, 1, 5))       # rolls to next year

    def test_times(self):
        self.assertEqual(parse_time("7pm"), time(19, 0))
        self.assertEqual(parse_time("7:30 PM"), time(19, 30))
        self.assertEqual(parse_time("19:30"), time(19, 30))
        self.assertEqual(parse_time("1930"), time(19, 30))
        self.assertEqual(parse_time("8"), time(20, 0))
        self.assertEqual(parse_time("noon"), time(12, 0))

    def test_ambiguous_times_return_both_readings(self):
        self.assertEqual(time_candidates("9"), [time(21, 0), time(9, 0)])
        self.assertEqual(time_candidates("9:30"), [time(21, 30), time(9, 30)])
        self.assertEqual(time_candidates("11"), [time(11, 0), time(23, 0)])
        self.assertEqual(time_candidates("09:00"), [time(9, 0)])          # leading zero = explicit
        self.assertEqual(time_candidates("10:00"), [time(10, 0)])         # HH:MM = 24h clock
        self.assertEqual(time_candidates("10"), [time(22, 0), time(10, 0)])
        self.assertEqual(time_candidates("breakfast at 9"), [time(9, 0)])
        self.assertEqual(time_candidates("kal raat 8 baje"), [time(20, 0)])
        self.assertEqual(time_candidates("19:30:00"), [time(19, 30)])

    def test_hindi_relative_days_are_future(self):
        self.assertEqual(parse_date("kal", TODAY), date(2026, 10, 13))
        self.assertEqual(parse_date("parso", TODAY), date(2026, 10, 14))

    def test_garbage(self):
        for bad in ["someday", "", "31/02"]:
            with self.assertRaises(ParseError):
                parse_date(bad, TODAY)
        with self.assertRaises(ParseError):
            parse_time("25:00")


class DataTests(unittest.TestCase):
    def test_seed_is_deterministic_and_in_range(self):
        a, b = generate_restaurants(), generate_restaurants()
        self.assertEqual(a, b)
        self.assertTrue(50 <= len(a) <= 100)
        self.assertEqual(len({r["id"] for r in a}), len(a))
        self.assertGreater(len({r["area"] for r in a}), 10)
        for r in a:
            self.assertEqual(r["total_seats"], sum(int(k) * v for k, v in r["tables"].items()))
            if r["veg_only"]:
                self.assertNotIn("bar", r["features"])


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.s = fresh_service()
        # an outlet with >=2 eight-seaters that is open at 19:00
        self.big = next(r["id"] for r in self.s._all_restaurants()
                        if r["tables"]["8"] >= 2 and r["open_time"] <= "12:00")

    def test_phone_normalisation(self):
        self.assertEqual(normalize_phone("098765 43210"), "+919876543210")
        self.assertEqual(normalize_phone("+91-98765-43210"), "+919876543210")
        with self.assertRaises(ToolError):
            normalize_phone("12345")

    def test_book_modify_cancel_lifecycle(self):
        res = self.s.make_reservation("BLR-001", "tomorrow", "7:30 pm", 4, "Asha Rao", "9876543210")
        code = res["confirmation_code"]
        self.assertRegex(code, r"^GF-[A-Z0-9]{6}$")
        self.assertEqual(self.s.modify_reservation(code, "9876543210", new_time="21:00")["reservation"]["time"], "21:00")
        self.assertEqual(self.s.cancel_reservation(code, "9876543210")["status"], "cancelled")
        with self.assertRaises(ToolError) as ctx:
            self.s.cancel_reservation(code, "9876543210")
        self.assertEqual(ctx.exception.code, "ALREADY_CANCELLED")

    def test_dry_run_writes_nothing(self):
        self.s.make_reservation("BLR-001", "tomorrow", "20:00", 2, "Asha Rao", "9876543210", dry_run=True)
        self.assertEqual(self.s.db.query("SELECT COUNT(*) n FROM reservations")[0]["n"], 0)

    def test_identity_verification_does_not_leak(self):
        code = self.s.make_reservation("BLR-001", "tomorrow", "20:00", 2, "Asha Rao", "9876543210")["confirmation_code"]
        with self.assertRaises(ToolError) as wrong_phone:
            self.s.lookup_reservation("9123456789", code)
        with self.assertRaises(ToolError) as no_code:
            self.s.lookup_reservation("9876543210", "GF-ZZZZZZ")
        self.assertEqual(wrong_phone.exception.message, no_code.exception.message)

    def test_capacity_is_never_oversold(self):
        r = self.s.resolve_restaurant(self.big)
        eights = r["tables"]["8"]
        made = 0
        for i in range(eights + 3):
            try:
                self.s.make_reservation(self.big, "tomorrow", "20:00", 8, "Guest", f"98765{i:05d}")
                made += 1
            except ToolError as exc:
                self.assertEqual(exc.code, "NOT_AVAILABLE")
                self.assertIn("alternative_times_same_day", exc.extra)
        self.assertEqual(made, eights)

    def test_overlapping_sittings_share_tables_but_later_ones_free_up(self):
        r = self.s.resolve_restaurant(self.big)
        for i in range(r["tables"]["8"]):
            self.s.make_reservation(self.big, "tomorrow", "19:00", 8, "Guest", f"98765{i:05d}")
        self.assertFalse(self.s.check_availability(self.big, "tomorrow", "20:00", 8)["available"])
        self.assertTrue(self.s.check_availability(self.big, "tomorrow", "20:30", 8)["available"])

    def test_duplicate_booking_blocked(self):
        self.s.make_reservation("BLR-001", "tomorrow", "20:00", 2, "Asha Rao", "9876543210")
        with self.assertRaises(ToolError) as ctx:
            self.s.make_reservation("BLR-002", "tomorrow", "20:30", 2, "Asha Rao", "9876543210")
        self.assertEqual(ctx.exception.code, "DUPLICATE_BOOKING")

    def test_rules(self):
        cases = [(("BLR-001", "2026-10-12", "10:00", 2), "TIME_IN_PAST"),
                 (("BLR-001", "tomorrow", "03:30", 2), "OUTSIDE_HOURS"),
                 (("BLR-001", "2027-03-01", "20:00", 2), "TOO_FAR_AHEAD"),
                 (("BLR-001", "tomorrow", "20:00", 40), "PARTY_TOO_LARGE"),
                 (("nowhere", "tomorrow", "20:00", 2), "RESTAURANT_NOT_FOUND")]
        for args, code in cases:
            with self.assertRaises(ToolError, msg=code) as ctx:
                self.s.check_availability(*args)
            self.assertEqual(ctx.exception.code, code)

    def _outlet(self, pred):
        return next(r for r in self.s._all_restaurants() if pred(r))

    def test_bare_hour_resolved_by_outlet_hours(self):
        dinner_only = self._outlet(lambda r: r["open_time"] >= "17:00")
        out = self.s.check_availability(dinner_only["id"], "tomorrow", "9", 2)
        self.assertEqual(out["time"], "21:00")
        self.assertTrue(any("only time" in n for n in out["notes"]))

    def test_bare_hour_asks_when_both_readings_are_open(self):
        cafe = self._outlet(lambda r: r["open_time"] <= "08:00" and r["close_time"] >= "22:30")
        with self.assertRaises(ToolError) as ctx:
            self.s.check_availability(cafe["id"], "tomorrow", "9", 2)
        self.assertEqual(ctx.exception.code, "AMBIGUOUS_TIME")
        self.assertEqual(self.s.check_availability(cafe["id"], "tomorrow", "9 am", 2)["time"], "09:00")

    def test_rounding_is_reported_not_silent(self):
        out = self.s.check_availability("BLR-001", "tomorrow", "19:45", 2)
        self.assertEqual(out["time"], "20:00")
        self.assertTrue(any("nearest slot" in n for n in out["notes"]))
        preview = self.s.make_reservation("BLR-001", "tomorrow", "19:45", 2, "Asha Rao", "9876543210", dry_run=True)
        self.assertTrue(preview["notes"])

    def test_time_without_date_means_today_and_is_checked(self):
        # clock is 16:00 today: 10:00 has passed, 20:00 has not
        for call in (lambda: self.s.recommend_restaurants(time="10:00"),
                     lambda: self.s.search_restaurants(cuisine="italian", time="10:00")):
            with self.assertRaises(ToolError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, "TIME_IN_PAST")
        rows = self.s.recommend_restaurants(time="20:00")["recommendations"]
        self.assertTrue(all("available_at_requested_time" in r for r in rows))

    def test_large_party_needs_private_room(self):
        rec = self.s.recommend_restaurants(occasion="birthday", party_size=15, date="saturday", time="20:00")
        for row in rec["recommendations"]:
            r = self.s.resolve_restaurant(row["restaurant_id"])
            self.assertGreaterEqual(r["private_dining_capacity"], 15)

    def test_search_filters_and_widening(self):
        out = self.s.search_restaurants(cuisine="sushi")
        self.assertTrue(all("Japanese" in r["cuisines"] for r in out["restaurants"]))
        veg = self.s.search_restaurants(veg_only=True, limit=10)
        self.assertTrue(veg["restaurants"])
        self.assertTrue(all(self.s.resolve_restaurant(r["restaurant_id"])["veg_only"] for r in veg["restaurants"]))
        with self.assertRaises(ToolError):
            self.s.search_restaurants(area="Mumbai")

    def test_recommendation_respects_occasion_and_budget(self):
        rec = self.s.recommend_restaurants(occasion="anniversary", budget_per_person=2000, party_size=2)
        top = rec["recommendations"][0]
        self.assertEqual(rec["occasion_detected"], "date")
        self.assertLessEqual(top["price_per_person_inr"], 2000)
        self.assertTrue(top["why"])

    def test_named_features_are_required(self):
        rec = self.s.recommend_restaurants(occasion="date", preferences="rooftop", party_size=2)
        for row in rec["recommendations"]:
            self.assertIn("rooftop", self.s.resolve_restaurant(row["restaurant_id"])["features"])

    def test_name_resolution(self):
        self.assertEqual(self.s.resolve_restaurant("sakura hsr")["area"], "HSR Layout")
        with self.assertRaises(ToolError) as ctx:
            self.s.resolve_restaurant("tandoor")
        self.assertEqual(ctx.exception.code, "AMBIGUOUS_RESTAURANT")


class MCPProtocolTests(unittest.TestCase):
    def setUp(self):
        self.server = MCPServer(fresh_service())
        self.client = MCPClient(InProcessTransport(self.server)).initialize()

    def test_handshake_and_listing(self):
        self.assertEqual(self.client.server_info["name"], "goodfoods-reservations")
        tools = {t["name"]: t for t in self.client.list_tools()}
        self.assertEqual(len(tools), 8)
        self.assertTrue(tools["search_restaurants"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["make_reservation"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["cancel_reservation"]["annotations"]["destructiveHint"])

    def test_tool_errors_are_results_not_crashes(self):
        payload, is_error = self.client.call_tool("check_availability", {"restaurant_id": "BLR-001"})
        self.assertTrue(is_error)
        self.assertEqual(payload["error"], "MISSING_ARGUMENTS")

    def test_argument_coercion(self):
        payload, is_error = self.client.call_tool(
            "check_availability", {"restaurant_id": "BLR-001", "date": "tomorrow", "time": "20:00",
                                   "party_size": "4", "bogus": 1})
        self.assertFalse(is_error, payload)
        self.assertEqual(payload["party_size"], 4)

    def test_protocol_errors(self):
        self.assertEqual(self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "nope"})["error"]["code"], -32601)
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        bad = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "x"}})
        self.assertEqual(bad["error"]["code"], -32602)


class SalvageTests(unittest.TestCase):
    TOOLS = {"search_restaurants", "make_reservation"}

    def test_llama_function_tag(self):
        calls = parse_text_tool_calls('<function=search_restaurants>{"cuisine": "italian", "area": "HSR"}</function>',
                                      self.TOOLS)
        self.assertEqual(calls, [{"name": "search_restaurants", "arguments": {"cuisine": "italian", "area": "HSR"}}])

    def test_json_blob_with_nested_and_escaped(self):
        text = 'Sure! {"name": "make_reservation", "parameters": {"customer_name": "A \\"B\\"", "party_size": 2}}'
        calls = parse_text_tool_calls(text, self.TOOLS)
        self.assertEqual(calls[0]["arguments"]["customer_name"], 'A "B"')

    def test_ignores_unknown_and_prose(self):
        self.assertEqual(parse_text_tool_calls('{"name": "rm_rf", "parameters": {}}', self.TOOLS), [])
        self.assertEqual(parse_text_tool_calls("Happy to help {with} that", self.TOOLS), [])


if __name__ == "__main__":
    unittest.main()
