"""The reservation agent: a from-scratch tool-calling loop (no LangChain / LlamaIndex).

    user msg -> [LLM decides: reply or tool call] -> MCP tools/call -> result back to LLM -> ... -> reply

Intent is decided by the LLM (it picks which tool to call, or none). Deterministic code
wraps it with guardrails that a small model cannot be trusted to hold on its own:

* Human-in-the-loop gate: any tool the MCP server marks as non-read-only is first executed
  as a dry run; the real write only happens after the guest explicitly confirms *that exact*
  action (same normalized summary). The LLM cannot book on its own initiative.
* Hallucination guard: a reply quoting a GF-XXXXXX code that no tool ever returned is rejected
  and regenerated.
* Loop guard: bounded steps, duplicate-call short-circuit, graceful fallback messages.
* Robust parsing: malformed JSON arguments are returned to the model as a fixable error.
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
from .services import CODE_RE

MAX_STEPS = 6
HISTORY_LIMIT = 16
OLD_TOOL_RESULT_CHARS = 500   # tool results from earlier turns are shortened to save tokens
MAX_TOOL_RESULT_CHARS = 3500

AFFIRM_RE = re.compile(
    r"^\s*(yes|yeah|yep|yup|ya|sure|ok(ay)?|confirm(ed)?|go ahead|please do|do it|book it|book|"
    r"sounds good|perfect|correct|haan|han|ha|ho|houdu|theek hai|thik hai|done)\b", re.I)
NEGATE_RE = re.compile(r"\b(no|nope|nah|don'?t|do not|wait|stop|change|instead|but|actually|nahi|beda)\b", re.I)


def is_affirmative(text: str) -> bool:
    """Deterministic confirmation check. Deliberately strict: 'yes but make it 9pm' is NOT
    a confirmation of the previous summary."""
    return bool(AFFIRM_RE.search(text or "")) and not NEGATE_RE.search(text or "") and len(text) <= 60


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

    def chat(self, user_text: str) -> AgentTurn:
        trace: list[TraceEvent] = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        user_text = (user_text or "").strip()[:1000]
        if not user_text:
            return AgentTurn("Could you tell me what you'd like to do?", trace, usage)

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

        for _ in range(MAX_STEPS):
            try:
                resp = self.llm.chat(self._messages(), self.tools)
            except LLMError as exc:
                trace.append(TraceEvent("llm", "error", {"message": str(exc)}))
                reply = ("Sorry, I'm having trouble reaching my reservation brain right now. "
                         "Please try again in a moment, or call your nearest GoodFoods outlet.")
                if "Rate limit" in str(exc):
                    m = re.search(r"wait (\d+)s", str(exc))
                    wait_min = max(1, round(int(m.group(1)) / 60)) if m else 1
                    if "per day" in str(exc) or "TPD" in str(exc) or wait_min >= 2:
                        reply = (f"Today's model allowance is used up for now; it frees up again in about "
                                 f"{wait_min} minute{'s' if wait_min != 1 else ''}. I still have your details, so "
                                 "just send your message again then — nothing has been booked or changed.")
                    else:
                        reply = ("I'm handling a lot of requests right now. Please wait about a minute and send "
                                 "that again — I still have your details, and nothing has been booked or changed.")
                if any(w in str(exc) for w in ("API key", "HTTP 401", "HTTP 403", "HTTP 404")):
                    reply = f"Setup issue: {exc}"
                # Roll back this turn so a retry starts clean (no half-finished tool exchange).
                # Keep the guest's own message (their name, phone or choice must not be lost) but drop
                # any half-finished tool exchange, so the retry starts from a clean state.
                del self.history[turn_start + 1:]
                return AgentTurn(reply, trace, usage)
            for k in usage:
                usage[k] += int(resp.usage.get(k, 0) or 0)
            msg = resp.message
            trace.append(TraceEvent("llm", "completion", {"tool_calls": len(msg.get("tool_calls", [])),
                                                          "recovered_from_text": resp.recovered}, resp.latency_ms))

            if not msg.get("tool_calls"):
                reply = (msg.get("content") or "").strip()
                invented = set(CODE_RE.findall(reply)) - self.known_codes
                if invented and not corrected:
                    corrected = True
                    trace.append(TraceEvent("guard", "hallucinated_code_blocked", {"codes": sorted(invented)}))
                    self.history.append({"role": "user", "content": HALLUCINATION_CORRECTION.format(
                        codes=", ".join(sorted(invented)))})
                    continue
                if not reply and not nudged:
                    # Reasoning models occasionally return an empty message; ask once more
                    # instead of showing the guest a dead end.
                    nudged = True
                    trace.append(TraceEvent("guard", "empty_reply_retried", {}))
                    self.history.append({"role": "user", "content": EMPTY_REPLY_NUDGE})
                    continue
                # drop internal correction notes from the history the model sees next turn
                self.history = [m for m in self.history if not str(m.get("content", "")).startswith("SYSTEM CHECK")]
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

    # ------------------------------------------------------------------ internals
    def _messages(self) -> list[dict]:
        system = {"role": "system", "content": render_system_prompt(self.clock(), self.n_outlets, self.city, self.guest_area)}
        recent = self.history[-HISTORY_LIMIT:]
        while recent and recent[0]["role"] != "user":   # never start on an orphan tool message
            recent = recent[1:]
        # Full tool results only for the current turn; earlier ones are trimmed. Every call counts
        # toward the provider's tokens-per-minute limit, and old search results rarely matter.
        last_user = max((i for i, m in enumerate(recent) if m["role"] == "user"), default=0)
        out = []
        for i, m in enumerate(recent):
            if m["role"] == "tool" and i < last_user and len(m["content"]) > OLD_TOOL_RESULT_CHARS:
                m = {**m, "content": m["content"][:OLD_TOOL_RESULT_CHARS] + "…(trimmed)"}
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
        seen[sig] = result
        trace.append(TraceEvent("tool", name, {"arguments": args, "result": result, "error": is_error},
                                (time.perf_counter() - started) * 1000))
        return result

    def _gated_write(self, name: str, args: dict, trace: list[TraceEvent]) -> tuple[dict, bool]:
        preview, is_error = self.mcp.call_tool(name, {**args, "dry_run": True})
        if is_error:
            return preview, True   # validation/availability problem: let the model fix it
        summary = {k: v for k, v in preview.items() if k not in ("status", "notes")}
        key = name + ":" + hashlib.sha256(json.dumps(summary, sort_keys=True, default=str).encode()).hexdigest()[:16]
        if key in self.approved:
            self.approved.discard(key)   # single use
            trace.append(TraceEvent("guard", "write_authorised", {"tool": name}))
            return self.mcp.call_tool(name, args)
        self.pending = PendingAction(name, key, summary)
        trace.append(TraceEvent("guard", "confirmation_required", {"tool": name}))
        return {"status": "needs_user_confirmation", "action": name, **summary,
                **({"notes": preview["notes"]} if preview.get("notes") else {}),
                "instruction": CONFIRMATION_INSTRUCTION}, False

    def _harvest_codes(self, obj) -> None:
        text = json.dumps(obj, default=str)
        self.known_codes.update(CODE_RE.findall(text))
