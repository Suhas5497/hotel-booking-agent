"""The reservation agent: a from-scratch tool-calling loop (no LangChain / LlamaIndex).

    user msg -> [LLM decides: reply or tool call] -> MCP tools/call -> result back to LLM -> ... -> reply

Intent is decided by the LLM (it picks which tool to call, or none). Deterministic code
wraps it with guardrails that a small model cannot be trusted to hold on its own:

* Human-in-the-loop gate: any tool the MCP server marks as non-read-only is first executed
  as a dry run; the real write only happens after the guest explicitly confirms *that exact*
  action. The approval key covers the resolved preview AND the unmasked identity fields
  (full phone, confirmation code), so the model cannot swap in a different phone number
  that happens to share the last four digits. Approvals only live for the turn in which the
  guest said yes.
* Hallucination guard: a reply quoting a GF-XXXXXX code that no tool ever returned is rejected
  and regenerated.
* Loop guard: bounded steps, duplicate-call short-circuit, graceful fallback messages.
* Robust parsing: malformed JSON arguments are returned to the model as a fixable error.
* Working memory: booking details the guest has already given (outlet, date, time, party,
  name, phone, code) are kept in agent state and restated in the system prompt, so they
  survive history trimming.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from .config import CITY, now as default_now
from .llm import LLMClient, LLMError
from .mcp_client import MCPClient
from .prompts import CONFIRMATION_INSTRUCTION, EMPTY_REPLY_NUDGE, HALLUCINATION_CORRECTION, render_system_prompt
from .services import CODE_RE, ToolError, normalize_phone

MAX_STEPS = 6
HISTORY_TURNS = 6             # keep the last N guest turns (with their tool exchanges) verbatim
OLD_TOOL_RESULT_CHARS = 500   # tool results from earlier turns are shortened to save tokens
MAX_TOOL_RESULT_CHARS = 3500

# Details worth remembering across turns, taken from the arguments of successful tool calls.
REMEMBERED_ARGS = {"restaurant_id": "restaurant_id", "date": "date", "time": "time", "party_size": "party_size",
                   "customer_name": "customer_name", "phone": "phone", "confirmation_code": "confirmation_code",
                   "new_date": "date", "new_time": "time", "new_party_size": "party_size", "new_name": "customer_name"}

AFFIRM_RE = re.compile(
    r"^\s*(yes|yeah|yep|yup|ya|sure|ok(ay)?|confirm(ed)?|go ahead|please do|do it|book it|book|"
    r"sounds good|perfect|correct|haan|han|ha|ho|houdu|theek hai|thik hai|done)\b", re.I)
NEGATE_RE = re.compile(r"\b(no|nope|nah|don'?t|do not|wait|stop|change|instead|but|actually|nahi|beda)\b", re.I)


def is_affirmative(text: str) -> bool:
    """Deterministic confirmation check. Deliberately strict: 'yes but make it 9pm' is NOT
    a confirmation of the previous summary."""
    text = text or ""
    return bool(AFFIRM_RE.search(text)) and not NEGATE_RE.search(text) and len(text) <= 60


def _identity(args: dict) -> dict:
    """Unmasked identity fields of a write call, normalised so formatting differences
    ("98765 43210" vs "+919876543210") don't force a needless re-confirmation."""
    out = {}
    if args.get("phone"):
        try:
            out["phone"] = normalize_phone(args["phone"])
        except ToolError:
            out["phone"] = str(args["phone"])
    if args.get("confirmation_code"):
        out["confirmation_code"] = str(args["confirmation_code"]).strip().upper()
    return out


@dataclass
class TraceEvent:
    kind: str                     # llm | tool | guard
    name: str
    detail: dict = field(default_factory=dict)
    ms: float = 0.0


@dataclass
class AgentTurn:
    reply: str
    trace: list[TraceEvent]
    usage: dict


@dataclass
class PendingAction:
    tool: str
    key: str
    summary: dict


class ReservationAgent:
    def __init__(self, llm: LLMClient, mcp: MCPClient, n_outlets: int = 75,
                 clock: Callable = default_now, city: str = CITY):
        self.llm, self.mcp, self.clock, self.city, self.n_outlets = llm, mcp, clock, city, n_outlets
        self.history: list[dict] = []
        self.guest_area: str | None = None   # set by the UI's "I'm near" picker
        self.pending: PendingAction | None = None
        self.approved: set[str] = set()
        self.known_codes: set[str] = set()
        self.known: dict[str, object] = {}   # working memory of details the guest already gave
        mcp_tools = mcp.list_tools()
        self.write_tools = {t["name"] for t in mcp_tools if not t.get("annotations", {}).get("readOnlyHint", True)}
        self.tools = [self._to_openai_schema(t) for t in mcp_tools]

    # ------------------------------------------------------------------ schema bridge
    @staticmethod
    def _to_openai_schema(tool: dict) -> dict:
        params = json.loads(json.dumps(tool["inputSchema"]))
        params.get("properties", {}).pop("dry_run", None)   # dry_run is ours to control, not the model's
        return {"type": "function", "function": {"name": tool["name"], "description": tool["description"],
                                                 "parameters": params}}

    # ------------------------------------------------------------------ public API
    def reset(self) -> None:
        self.history.clear()
        self.pending = None
        self.approved.clear()
        self.known_codes.clear()
        self.known.clear()

    def chat(self, user_text: str) -> AgentTurn:
        trace: list[TraceEvent] = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        user_text = (user_text or "").strip()[:1000]
        if not user_text:
            return AgentTurn("Could you tell me what you'd like to do?", trace, usage)

        # An approval is only valid in the turn the guest gave it. Anything left over from an
        # earlier turn (e.g. the model never re-issued the write) must not authorise a later call.
        self.approved.clear()
        if self.pending:
            if is_affirmative(user_text):
                self.approved.add(self.pending.key)
                trace.append(TraceEvent("guard", "confirmation_received", {"action": self.pending.tool}))
            self.pending = None   # any reply resolves the pending prompt; a "no" simply drops it

        self.known_codes.update(CODE_RE.findall(user_text.upper()))   # codes the guest typed are not inventions
        turn_start = len(self.history)
        self.history.append({"role": "user", "content": user_text})
        seen_calls: dict[str, dict] = {}
        corrected = False
        nudged = False

        try:
            for _ in range(MAX_STEPS):
                try:
                    resp = self.llm.chat(self._messages(), self.tools)
                except LLMError as exc:
                    trace.append(TraceEvent("llm", "error", {"message": str(exc)}))
                    # Keep the guest's own message (their name, phone or choice must not be lost) but drop
                    # any half-finished tool exchange, so the retry starts from a clean state.
                    del self.history[turn_start + 1:]
                    return AgentTurn(self._error_reply(exc), trace, usage)
                for k in usage:
                    usage[k] += int(resp.usage.get(k, 0) or 0)
                msg = resp.message
                trace.append(TraceEvent("llm", "completion", {"tool_calls": len(msg.get("tool_calls", [])),
                                                              "recovered_from_text": resp.recovered},
                                        resp.latency_ms))

                if not msg.get("tool_calls"):
                    reply = (msg.get("content") or "").strip()
                    invented = set(CODE_RE.findall(reply)) - self.known_codes
                    if invented and not corrected:
                        corrected = True
                        trace.append(TraceEvent("guard", "hallucinated_code_blocked", {"codes": sorted(invented)}))
                        self._internal_note(HALLUCINATION_CORRECTION.format(codes=", ".join(sorted(invented))))
                        continue
                    if not reply and not nudged:
                        # Reasoning models occasionally return an empty message; ask once more
                        # instead of showing the guest a dead end.
                        nudged = True
                        trace.append(TraceEvent("guard", "empty_reply_retried", {}))
                        self._internal_note(EMPTY_REPLY_NUDGE)
                        continue
                    reply = reply or ("Sorry, I lost my train of thought there. Could you tell me the area, date, "
                                      "time and number of guests again?")
                    self.history.append({"role": "assistant", "content": reply})
                    return AgentTurn(reply, trace, usage)

                # Execute tool calls (sequentially; parallel calls disabled at the API).
                self.history.append({"role": "assistant", "content": msg.get("content") or "",
                                     "tool_calls": msg["tool_calls"]})
                for call in msg["tool_calls"]:
                    result = self._run_tool(call, seen_calls, trace)
                    self.history.append({"role": "tool", "tool_call_id": call.get("id", "call"),
                                         "content": json.dumps(result, ensure_ascii=False)[:MAX_TOOL_RESULT_CHARS]})

            reply = ("I've gathered the details but couldn't finish that in one go. "
                     "Could you tell me the outlet, date, time and number of guests once more?")
            self.history.append({"role": "assistant", "content": reply})
            return AgentTurn(reply, trace, usage)
        finally:
            # Internal correction notes are for this turn only; the model shouldn't see them again.
            self.history = [m for m in self.history if not m.get("internal")]
            self.approved.clear()

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _error_reply(exc: LLMError) -> str:
        text = str(exc)
        if any(w in text for w in ("API key", "HTTP 401", "HTTP 403", "HTTP 404")):
            return f"Setup issue: {exc}"
        if "Rate limit" in text:
            m = re.search(r"wait (\d+)s", text)
            wait_min = max(1, round(int(m.group(1)) / 60)) if m else 1
            if "per day" in text or "TPD" in text or wait_min >= 2:
                return (f"Today's model allowance is used up for now; it frees up again in about "
                        f"{wait_min} minute{'s' if wait_min != 1 else ''}. I still have your details, so "
                        "just send your message again then — nothing has been booked or changed.")
            return ("I'm handling a lot of requests right now. Please wait about a minute and send "
                    "that again — I still have your details, and nothing has been booked or changed.")
        return ("Sorry, I'm having trouble reaching my reservation brain right now. "
                "Please try again in a moment, or call your nearest GoodFoods outlet.")

    def _internal_note(self, text: str) -> None:
        """A correction for the model. Flagged so it is never mistaken for (or confused with)
        something the guest typed, and removed when the turn ends."""
        self.history.append({"role": "user", "content": text, "internal": True})

    def _messages(self) -> list[dict]:
        system = {"role": "system", "content": render_system_prompt(
            self.clock(), self.n_outlets, self.city, self.guest_area, known=self.known)}
        # Keep whole turns: start at the Nth-last real guest message so a tool result is never
        # separated from the call that produced it.
        user_idx = [i for i, m in enumerate(self.history) if m["role"] == "user" and not m.get("internal")]
        start = user_idx[-HISTORY_TURNS] if len(user_idx) > HISTORY_TURNS else 0
        recent = self.history[start:]
        # Full tool results only for the current turn; earlier ones are trimmed. Every call counts
        # toward the provider's tokens-per-minute limit, and old search results rarely matter.
        last_user = max((i for i, m in enumerate(recent) if m["role"] == "user" and not m.get("internal")),
                        default=0)
        out = []
        for i, m in enumerate(recent):
            m = {k: v for k, v in m.items() if k != "internal"}   # never send our own flags to the API
            if m["role"] == "tool" and i < last_user and len(m["content"]) > OLD_TOOL_RESULT_CHARS:
                m["content"] = m["content"][:OLD_TOOL_RESULT_CHARS] + "…(trimmed)"
            out.append(m)
        return [system] + out

    def _run_tool(self, call: dict, seen: dict, trace: list[TraceEvent]) -> dict:
        fn = call.get("function", {})
        name = fn.get("name", "")
        raw = fn.get("arguments") or "{}"
        started = time.perf_counter()
        try:
            args = raw if isinstance(raw, dict) else json.loads(raw)
            if not isinstance(args, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            result = {"error": "BAD_ARGUMENTS", "message": "Tool arguments were not valid JSON.",
                      "hint": "Call the tool again with a JSON object of arguments."}
            trace.append(TraceEvent("tool", name, {"arguments": raw, "result": result, "error": True}))
            return result
        args.pop("dry_run", None)

        sig = name + json.dumps(args, sort_keys=True)
        if sig in seen:   # model looping on the same call -> return cached result with a nudge
            trace.append(TraceEvent("guard", "duplicate_call_short_circuit", {"tool": name}))
            return {**seen[sig], "note": "Same call already made this turn; use this result."}

        if name in self.write_tools:
            result, is_error = self._gated_write(name, args, trace)
        else:
            result, is_error = self.mcp.call_tool(name, args)
        self._harvest_codes(result)
        if not is_error:
            self._remember(args, result)
        seen[sig] = result
        trace.append(TraceEvent("tool", name, {"arguments": args, "result": result, "error": is_error},
                                (time.perf_counter() - started) * 1000))
        return result

    def _gated_write(self, name: str, args: dict, trace: list[TraceEvent]) -> tuple[dict, bool]:
        preview, is_error = self.mcp.call_tool(name, {**args, "dry_run": True})
        if is_error:
            return preview, True   # validation/availability problem: let the model fix it
        summary = {k: v for k, v in preview.items() if k not in ("status", "notes")}
        # The preview shows a masked phone (+91 ******3210); hash the full identity too, otherwise
        # a different number with the same last four digits would match the guest's approval.
        fingerprint = {"preview": summary, "identity": _identity(args)}
        key = name + ":" + hashlib.sha256(
            json.dumps(fingerprint, sort_keys=True, default=str).encode()).hexdigest()[:16]
        if key in self.approved:
            self.approved.discard(key)   # single use
            trace.append(TraceEvent("guard", "write_authorised", {"tool": name}))
            return self.mcp.call_tool(name, args)
        self.pending = PendingAction(name, key, summary)
        trace.append(TraceEvent("guard", "confirmation_required", {"tool": name}))
        return {"status": "needs_user_confirmation", "action": name, **summary,
                **({"notes": preview["notes"]} if preview.get("notes") else {}),
                "instruction": CONFIRMATION_INSTRUCTION}, False

    def _remember(self, args: dict, result: dict) -> None:
        for arg, slot in REMEMBERED_ARGS.items():
            if args.get(arg) not in (None, "", []):
                self.known[slot] = args[arg]
        codes = CODE_RE.findall(json.dumps(result.get("confirmation_code", ""), default=str))
        if codes:
            self.known["confirmation_code"] = codes[0]
        if result.get("status") == "cancelled":
            self.known.pop("confirmation_code", None)

    def _harvest_codes(self, obj) -> None:
        text = json.dumps(obj, default=str)
        self.known_codes.update(CODE_RE.findall(text))
