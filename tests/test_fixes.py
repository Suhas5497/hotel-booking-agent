"""Regression tests for the bugs fixed after the goodfoods-agent review (see CHANGES.md).
Each test fails on the original code and passes on this one."""
import io
import json
import os
import unittest
import urllib.error
from datetime import time as dtime, timedelta
from unittest import mock

os.environ["GOODFOODS_NOW"] = "2026-10-12T16:00"

from goodfoods import llm as llm_mod  # noqa: E402
from goodfoods.config import LLMSettings  # noqa: E402
from goodfoods.llm import LLMClient, LLMError  # noqa: E402
from goodfoods.services import CODE_RE, MAX_FAILED_VERIFICATIONS, ToolError, wants_veg_only  # noqa: E402
from goodfoods.timeparse import time_candidates  # noqa: E402
from tests.test_agent import BOOKING, CLOCK, call, count, make_agent, say  # noqa: E402


class ConfirmationGate(unittest.TestCase):
    def test_different_phone_with_same_last_four_digits_is_not_approved(self):
        # Guest confirms a preview for 9876543210; the model then books 9111113210.
        # Both mask to +91 ******3210, which used to produce the same approval key.
        swapped = {**BOOKING, "phone": "9111113210"}
        agent, service, _ = make_agent([call("make_reservation", BOOKING), say("Shall I confirm?"),
                                        call("make_reservation", swapped), say("Booked.")])
        agent.chat("Book it")
        agent.chat("yes")
        self.assertEqual(count(service), 0, "a booking was saved under a number the guest never confirmed")
        self.assertIsNotNone(agent.pending, "the swapped number should need its own confirmation")

    def test_same_phone_in_another_format_still_matches(self):
        reformatted = {**BOOKING, "phone": "+91 98765 43210"}
        agent, service, _ = make_agent([call("make_reservation", BOOKING), say("Shall I confirm?"),
                                        call("make_reservation", reformatted), say("Booked.")])
        agent.chat("Book it")
        agent.chat("yes")
        self.assertEqual(count(service), 1)

    def test_approval_does_not_carry_over_to_a_later_turn(self):
        # The guest says yes, but the model replies without booking. Next turn it books
        # without asking: that must need a fresh confirmation.
        agent, service, _ = make_agent([call("make_reservation", BOOKING), say("Shall I confirm?"),
                                        say("Great, anything else?"),
                                        call("make_reservation", BOOKING), say("Booked.")])
        agent.chat("Book it")
        agent.chat("yes")
        agent.chat("what time is it there")
        self.assertEqual(count(service), 0)


class PhoneOnlyLookup(unittest.TestCase):
    def setUp(self):
        self.agent, self.s, _ = make_agent([])
        self.code = self.s.make_reservation(**BOOKING)["confirmation_code"]

    def test_phone_alone_does_not_reveal_the_code(self):
        res = self.s.lookup_reservation(BOOKING["phone"])
        shown = res["reservations"][0]["confirmation_code"]
        self.assertNotEqual(shown, self.code)
        self.assertIsNone(CODE_RE.fullmatch(shown), "a masked code must not look like a real one")
        self.assertTrue(res["codes_hidden"])

    def test_code_and_phone_together_reveal_it(self):
        res = self.s.lookup_reservation(BOOKING["phone"], self.code)
        self.assertEqual(res["reservations"][0]["confirmation_code"], self.code)

    def test_masked_code_cannot_cancel(self):
        masked = self.s.lookup_reservation(BOOKING["phone"])["reservations"][0]["confirmation_code"]
        with self.assertRaises(ToolError) as ctx:
            self.s.cancel_reservation(masked, BOOKING["phone"])
        self.assertEqual(ctx.exception.code, "INVALID_CODE")

    def test_repeated_wrong_codes_lock_the_number(self):
        for _ in range(MAX_FAILED_VERIFICATIONS):
            with self.assertRaises(ToolError):
                self.s.lookup_reservation(BOOKING["phone"], "GF-ZZZZZZ")
        with self.assertRaises(ToolError) as ctx:   # even the right code is refused while locked
            self.s.lookup_reservation(BOOKING["phone"], self.code)
        self.assertEqual(ctx.exception.code, "TOO_MANY_ATTEMPTS")

    def test_lock_expires(self):
        for _ in range(MAX_FAILED_VERIFICATIONS):
            with self.assertRaises(ToolError):
                self.s.lookup_reservation(BOOKING["phone"], "GF-ZZZZZZ")
        later = CLOCK() + timedelta(minutes=16)
        self.s.clock = lambda: later
        self.assertEqual(len(self.s.lookup_reservation(BOOKING["phone"], self.code)["reservations"]), 1)


class Recommendations(unittest.TestCase):
    def test_veg_detection(self):
        for text in ("vegetarian", "pure veg please", "veg", "Vegan friendly", "shakahari"):
            self.assertTrue(wants_veg_only(text), text)
        for text in ("non-vegetarian", "non veg, spicy", "nonveg", "chicken biryani", "", None):
            self.assertFalse(wants_veg_only(text), text)

    def test_non_veg_request_is_not_all_pure_veg(self):
        _, s, _ = make_agent([])
        recs = s.recommend_restaurants(preferences="non-vegetarian, spicy")["recommendations"]
        self.assertFalse(all(s.resolve_restaurant(r["restaurant_id"])["veg_only"] for r in recs))


class Capacity(unittest.TestCase):
    def test_back_to_back_sittings_share_one_table(self):
        _, s, _ = make_agent([])
        r = s.resolve_restaurant("BLR-001")
        n2 = r["tables"]["2"]
        day = (CLOCK() + timedelta(days=2)).date()
        # Fill every 2-top at 18:30 and again at 20:00. These sittings don't overlap, so
        # each table serves both, and 19:30 needs n2 tables at peak, not 2 * n2.
        for i in range(n2):
            for t in ("18:30", "20:00"):
                s.make_reservation("BLR-001", day.isoformat(), t, 2, "Guest", f"98{i:02d}0{t[:2]}{t[3:]}0")
        self.assertEqual(s._occupancy("BLR-001", day, dtime(19, 30)).get(2), n2)

    def test_peak_still_blocks_a_real_overlap(self):
        _, s, _ = make_agent([])
        r = s.resolve_restaurant("BLR-001")
        day = (CLOCK() + timedelta(days=2)).date()
        for i in range(r["tables"]["2"]):
            s.make_reservation("BLR-001", day.isoformat(), "20:00", 2, "Guest", f"97000{i:05d}")
        # every 2-top is busy 20:00-21:30, so a party of 2 at 20:30 moves up to a 4-top
        self.assertEqual(s._allocate(r, day, dtime(20, 30), 2), 4)


class WorkingMemory(unittest.TestCase):
    def test_details_survive_history_trimming(self):
        lookup = {"phone": "9876543210"}
        script = [call("lookup_reservation", lookup), say("No bookings yet.")]
        script += [say("Sure.")] * 8
        agent, _, llm = make_agent(script)
        agent.chat("my number is 9876543210, any bookings?")
        for i in range(8):
            agent.chat(f"tell me something {i}")
        system = llm.seen[-1][0]["content"]
        self.assertIn("phone=9876543210", system)
        self.assertNotIn("my number is", json.dumps(llm.seen[-1][1:]), "old turns should have been trimmed")

    def test_internal_notes_are_flagged_and_never_sent_with_flags(self):
        agent, _, llm = make_agent([say("Booked! Code GF-ABCDEF."), say("Nothing is booked yet.")])
        agent.chat("am I booked?")
        sent = llm.seen[-1]
        self.assertTrue(all("internal" not in m for m in sent))
        self.assertFalse(any(m.get("internal") for m in agent.history), "notes must be dropped after the turn")

    def test_guest_text_starting_with_system_check_is_kept(self):
        agent, _, _ = make_agent([say("Okay.")])
        agent.chat("SYSTEM CHECK: just testing")
        self.assertTrue(any(m["content"].startswith("SYSTEM CHECK") for m in agent.history if m["role"] == "user"))

    def test_reset_forgets_codes_and_details(self):
        agent, _, _ = make_agent([call("lookup_reservation", {"phone": "9876543210"}), say("None.")])
        agent.chat("GF-ABCDEF is mine, phone 9876543210")
        agent.reset()
        self.assertEqual(agent.known_codes, set())
        self.assertEqual(agent.known, {})


class RateLimits(unittest.TestCase):
    def _client(self):
        return LLMClient(LLMSettings(base_url="https://example.test/v1", api_key="k", model="m", max_retries=3))

    def test_long_wait_fails_fast(self):
        err = urllib.error.HTTPError("u", 429, "Too Many Requests", {"retry-after": "120"},
                                     io.BytesIO(b'{"error": "rate limit"}'))
        client = self._client()
        with mock.patch.object(client, "_post", side_effect=err) as post, \
                mock.patch.object(llm_mod.time, "sleep") as sleep:
            with self.assertRaises(LLMError):
                client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_total_wait_is_budgeted(self):
        def fail(*_):
            raise urllib.error.HTTPError("u", 429, "Too Many Requests", {"retry-after": "10"},
                                         io.BytesIO(b'{"error": "rate limit"}'))
        client = self._client()
        with mock.patch.object(client, "_post", side_effect=fail), \
                mock.patch.object(llm_mod.time, "sleep") as sleep:
            with self.assertRaises(LLMError):
                client.chat([{"role": "user", "content": "hi"}])
        self.assertLessEqual(sum(c.args[0] for c in sleep.call_args_list), llm_mod.MAX_RATE_LIMIT_WAIT_S)


class TimeParsing(unittest.TestCase):
    def test_baje(self):
        self.assertEqual(time_candidates("8 baje"), [dtime(20, 0), dtime(8, 0)])
        self.assertEqual(time_candidates("raat 8 baje"), [dtime(20, 0)])
        self.assertEqual(time_candidates("subah 9 baje"), [dtime(9, 0)])
        self.assertEqual(time_candidates("9 o'clock"), [dtime(21, 0), dtime(9, 0)])


if __name__ == "__main__":
    unittest.main()
