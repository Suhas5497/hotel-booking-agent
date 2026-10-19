"""Agent-loop tests with a scripted fake LLM: verifies the guardrails hold no matter
what the model does (books without consent, invents codes, loops, emits bad JSON)."""
import json
import os
import unittest
from datetime import datetime

os.environ["GOODFOODS_NOW"] = "2026-10-12T16:00"

from goodfoods.agent import ReservationAgent, is_affirmative  # noqa: E402
from goodfoods.config import TZ  # noqa: E402
from goodfoods.llm import LLMError, LLMResponse  # noqa: E402
from goodfoods.mcp_client import InProcessTransport, MCPClient  # noqa: E402
from goodfoods.mcp_server import MCPServer  # noqa: E402
from goodfoods.seed import build_database  # noqa: E402
from goodfoods.services import ReservationService  # noqa: E402

CLOCK = lambda: datetime(2026, 10, 12, 16, 0, tzinfo=TZ)  # noqa: E731
BOOKING = {"restaurant_id": "BLR-001", "date": "2026-10-13", "time": "20:00", "party_size": 2,
           "customer_name": "Asha Rao", "phone": "9876543210"}


def call(name, args, raw=False):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c_{name}", "type": "function",
         "function": {"name": name, "arguments": args if raw else json.dumps(args)}}]}


def say(text):
    return {"role": "assistant", "content": text}


class FakeLLM:
    """Returns scripted messages in order; records what the agent sent."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def chat(self, messages, tools=None):
        self.seen.append(messages)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResponse(item, {"prompt_tokens": 10, "completion_tokens": 5}, 1.0)


def make_agent(script):
    service = ReservationService(build_database(":memory:", with_bookings=False), clock=CLOCK)
    mcp = MCPClient(InProcessTransport(MCPServer(service))).initialize()
    llm = FakeLLM(script)
    return ReservationAgent(llm, mcp, n_outlets=75, clock=CLOCK), service, llm


def count(service):
    return service.db.query("SELECT COUNT(*) n FROM reservations")[0]["n"]


def tool_results(agent):
    return [json.loads(m["content"]) for m in agent.history if m["role"] == "tool"]


class ConfirmationGateTests(unittest.TestCase):
    def test_booking_requires_explicit_yes(self):
        agent, service, _ = make_agent([
            call("make_reservation", BOOKING), say("Shall I book Biryani Co. for 2 at 8 PM tomorrow?"),
            call("make_reservation", BOOKING), say("Booked!"),
        ])
        agent.chat("Book Biryani Co Indiranagar tomorrow 8pm for 2, Asha Rao, 9876543210")
        self.assertEqual(count(service), 0, "model must not be able to book without consent")
        self.assertEqual(tool_results(agent)[-1]["status"], "needs_user_confirmation")

        turn = agent.chat("yes please")
        self.assertEqual(count(service), 1)
        code = tool_results(agent)[-1]["confirmation_code"]
        self.assertIn(code, agent.known_codes)
        self.assertTrue(any(e.name == "write_authorised" for e in turn.trace))

    def test_no_or_changed_details_do_not_authorise(self):
        changed = dict(BOOKING, time="21:00")
        agent, service, _ = make_agent([
            call("make_reservation", BOOKING), say("Confirm?"),
            call("make_reservation", changed), say("Confirm 9 PM?"),
        ])
        agent.chat("book it")
        agent.chat("yes but make it 9pm")          # not a clean yes -> must re-confirm
        self.assertEqual(count(service), 0)
        self.assertEqual(tool_results(agent)[-1]["status"], "needs_user_confirmation")

    def test_approval_is_bound_to_exact_action(self):
        other = dict(BOOKING, party_size=6)
        agent, service, _ = make_agent([
            call("make_reservation", BOOKING), say("Confirm for 2?"),
            call("make_reservation", other), say("..."),
        ])
        agent.chat("book")
        agent.chat("yes")                          # approved party of 2, model tries party of 6
        self.assertEqual(count(service), 0)

    def test_cancel_is_gated_too(self):
        agent, service, _ = make_agent([])
        code = service.make_reservation(**BOOKING)["confirmation_code"]
        agent.llm.script = [call("cancel_reservation", {"confirmation_code": code, "phone": "9876543210"}),
                            say("Cancel it?"),
                            call("cancel_reservation", {"confirmation_code": code, "phone": "9876543210"}),
                            say("Cancelled.")]
        agent.chat(f"cancel {code}, phone 9876543210")
        self.assertEqual(service.db.query("SELECT status FROM reservations")[0]["status"], "confirmed")
        agent.chat("yes")
        self.assertEqual(service.db.query("SELECT status FROM reservations")[0]["status"], "cancelled")


class RobustnessTests(unittest.TestCase):
    def test_hallucinated_code_is_blocked(self):
        agent, _, llm = make_agent([say("Done! Your code is GF-ABC123."), say("Nothing is booked yet. Shall I?")])
        turn = agent.chat("book a table")
        self.assertNotIn("GF-ABC123", turn.reply)
        self.assertTrue(any(e.name == "hallucinated_code_blocked" for e in turn.trace))
        self.assertFalse(any("SYSTEM CHECK" in str(m.get("content")) for m in agent.history))

    def test_empty_reply_is_retried_once(self):
        agent, _, _ = make_agent([say(""), say("Which area would you like?")])
        turn = agent.chat("Koramangala")
        self.assertEqual(turn.reply, "Which area would you like?")
        self.assertTrue(any(e.name == "empty_reply_retried" for e in turn.trace))
        self.assertFalse(any("SYSTEM CHECK" in str(m.get("content")) for m in agent.history))

    def test_repeated_empty_reply_falls_back_gracefully(self):
        agent, _, _ = make_agent([say(""), say("")])
        turn = agent.chat("Koramangala")
        self.assertIn("area, date", turn.reply)

    def test_rate_limit_gets_a_clear_reply_and_clean_history(self):
        agent, _, _ = make_agent([LLMError("Rate limit reached (HTTP 429). The provider asked to wait 40s.")])
        turn = agent.chat("Book Sakura HSR")
        self.assertIn("wait about a minute", turn.reply)
        self.assertEqual(agent.history, [{"role": "user", "content": "Book Sakura HSR"}])   # guest's words kept

    def test_daily_limit_message_and_details_survive(self):
        agent, _, llm = make_agent([
            LLMError("Rate limit reached (HTTP 429, tokens per day). The provider asked to wait 199s."),
            say("Thanks Asha — shall I confirm?")])
        first = agent.chat("Name Asha Rao, phone 9876543210")
        self.assertIn("about 3 minutes", first.reply)
        agent.chat("please book it")
        sent = " ".join(str(m.get("content")) for m in llm.seen[-1])
        self.assertIn("9876543210", sent)   # the model still sees the name and phone

    def test_old_tool_results_are_trimmed_in_prompt(self):
        agent, _, llm = make_agent([call("search_restaurants", {"limit": 10}), say("Here you go"), say("Sure")])
        agent.chat("show me everything")
        agent.chat("thanks")
        second_call = llm.seen[-1]
        old_tool = [m for m in second_call if m["role"] == "tool"][0]
        self.assertTrue(old_tool["content"].endswith("…(trimmed)"))

    def test_codes_typed_by_user_are_not_flagged(self):
        agent, _, _ = make_agent([say("Let me look up GF-ABC123 — what's your phone number?")])
        turn = agent.chat("what about booking GF-ABC123?")
        self.assertIn("GF-ABC123", turn.reply)

    def test_malformed_arguments_are_returned_as_fixable_error(self):
        agent, _, _ = make_agent([call("search_restaurants", "{cuisine: italian", raw=True),
                                  call("search_restaurants", {"cuisine": "italian"}), say("Here are options")])
        turn = agent.chat("italian?")
        results = tool_results(agent)
        self.assertEqual(results[0]["error"], "BAD_ARGUMENTS")
        self.assertGreater(results[1]["count"], 0)
        self.assertEqual(turn.reply, "Here are options")

    def test_unknown_tool(self):
        agent, _, _ = make_agent([call("drop_tables", {}), say("Sorry, I can only help with reservations.")])
        agent.chat("hack")
        self.assertEqual(tool_results(agent)[0]["error"], "TOOL_CALL_FAILED")

    def test_loop_is_bounded(self):
        agent, _, _ = make_agent([call("search_restaurants", {"cuisine": "x", "limit": i}) for i in range(10)])
        turn = agent.chat("loop forever")
        self.assertIn("couldn't finish", turn.reply)

    def test_llm_outage_degrades_gracefully(self):
        agent, _, _ = make_agent([LLMError("provider busy")])
        turn = agent.chat("hi")
        self.assertIn("trouble", turn.reply)

    def test_system_prompt_is_grounded_in_today(self):
        agent, _, llm = make_agent([say("hi")])
        agent.chat("hello")
        system = llm.seen[0][0]["content"]
        self.assertIn("Monday, 12 October 2026", system)
        self.assertNotIn("dry_run", json.dumps(agent.tools))   # internal param hidden from the model

    def test_affirmation_detector(self):
        for yes in ["yes", "Yes please", "ok", "go ahead", "haan", "confirm", "book it"]:
            self.assertTrue(is_affirmative(yes), yes)
        for no in ["no", "yes but 9pm", "wait", "actually change it", "what time is it?"]:
            self.assertFalse(is_affirmative(no), no)


class JourneyHarnessTests(unittest.TestCase):
    """The journey runner itself, driven by a scripted model (no API key needed)."""

    def test_direct_booking_journey_completes_with_consent(self):
        from evals.journeys import JOURNEYS, run_journey
        journey = next(j for j in JOURNEYS if j.name == "direct booking")
        args = dict(BOOKING, restaurant_id="BLR-016", date="2026-10-17", time="20:00")
        agent, service, _ = make_agent([
            say("Lovely choice! May I have the name and phone number for the booking?"),
            call("make_reservation", args), say("Sakura HSR, Sat 17 Oct, 8 PM for 2 — shall I confirm?"),
            call("make_reservation", args), say("You're booked!"),
        ])
        result = run_journey(journey, agent, service)
        self.assertTrue(result["success"], result["detail"])
        self.assertEqual(result["turns"], 3)                  # ask details -> preview -> "yes"
        self.assertEqual(result["transcript"][-1]["guest"], "yes")
        self.assertEqual(result["unconfirmed_writes"], 0)

    def test_journey_reports_failure_when_model_never_books(self):
        from evals.journeys import JOURNEYS, run_journey
        journey = next(j for j in JOURNEYS if j.name == "direct booking")
        agent, service, _ = make_agent([say("Sorry, could you repeat that?")] * 6)
        result = run_journey(journey, agent, service)
        self.assertFalse(result["success"])
        self.assertEqual(result["turns"], 6)


if __name__ == "__main__":
    unittest.main()
