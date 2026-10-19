"""OpenAI-compatible chat-completions client, written on the standard library.

Works unchanged against Groq, Together, Fireworks, OpenRouter, a self-hosted vLLM or
Ollama (`http://localhost:11434/v1`) — i.e. we can move from a hosted Llama 8B to
our own GPUs by changing LLM_BASE_URL. No SDK, no framework.
"""
from __future__ import annotations

import json
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .config import LLMSettings


class LLMError(Exception):
    pass


MAX_RATE_LIMIT_WAIT_S = 25   # total time one guest turn may spend waiting on rate limits


def _retry_after(exc, detail: str) -> float:
    """Seconds to wait before retrying a 429: Retry-After header, else 'try again in 6.2s' in the body."""
    header = exc.headers.get("retry-after") if getattr(exc, "headers", None) else None
    try:
        if header:
            return float(header)
    except ValueError:
        pass
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)(ms|s)", detail)
    if m:
        minutes = float(m.group(1) or 0)
        value = float(m.group(2)) / (1000 if m.group(3) == "ms" else 1)
        return minutes * 60 + value
    return 8.0


@dataclass
class LLMResponse:
    message: dict                       # {"role": "assistant", "content": ..., "tool_calls": [...]}
    usage: dict = field(default_factory=dict)
    latency_ms: float = 0.0
    recovered: bool = False             # tool call salvaged from a malformed generation


def _extract_json_object(text: str, start: int) -> tuple[str, int] | None:
    """Brace-matching extractor (handles nested objects and strings)."""
    i = text.find("{", start)
    if i < 0:
        return None
    depth, in_str, esc = 0, False, False
    for j in range(i, len(text)):
        ch = text[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1], j + 1
    return None


def parse_text_tool_calls(text: str, known_tools: set[str]) -> list[dict]:
    """Recover tool calls that Llama-family models sometimes emit as plain text, e.g.
    `<function=search_restaurants>{"cuisine": "italian"}</function>` or
    `{"name": "search_restaurants", "parameters": {...}}`."""
    calls = []
    if not text:
        return calls
    for m in re.finditer(r"<function[=\s]+([A-Za-z_][\w\-]*)", text):
        found = _extract_json_object(text, m.end())
        if m.group(1) in known_tools:
            try:
                args = json.loads(found[0]) if found else {}
            except json.JSONDecodeError:
                continue
            calls.append({"name": m.group(1), "arguments": args})
    if calls:
        return calls
    pos = 0
    while True:
        found = _extract_json_object(text, pos)
        if not found:
            break
        blob, pos = found
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "function" and isinstance(obj.get("function"), dict):
            obj = obj["function"]
        name = obj.get("name") if isinstance(obj, dict) else None
        if name in known_tools:
            args = obj.get("parameters", obj.get("arguments", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append({"name": name, "arguments": args if isinstance(args, dict) else {}})
    return calls


def _as_tool_calls(calls: list[dict]) -> list[dict]:
    return [{"id": f"call_r{i}_{random.randint(1000, 9999)}", "type": "function",
             "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
            for i, c in enumerate(calls)]


class LLMClient:
    def __init__(self, settings: LLMSettings):
        self.s = settings
        self.on_wait = None   # optional callback(seconds) so the UI can say "waiting for the model…"

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        if not self.s.api_key and "localhost" not in self.s.base_url and "127.0.0.1" not in self.s.base_url:
            raise LLMError("No LLM API key configured. Set LLM_API_KEY (or GROQ_API_KEY) in .env.")
        body = {"model": self.s.model, "messages": messages, "temperature": self.s.temperature,
                "max_tokens": 700}
        if "gpt-oss" in self.s.model or "qwen3" in self.s.model:
            # Reasoning models spend completion tokens on hidden reasoning first; keep it short
            # and leave room for the actual reply or tool call.
            body["max_tokens"] = 900   # counts toward the per-minute token limit, so keep it modest
            body["reasoning_effort"] = "low" if "gpt-oss" in self.s.model else "none"
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
            body["parallel_tool_calls"] = False   # one action at a time is easier to verify
        known = {t["function"]["name"] for t in tools or []}
        last_err = None
        waited = 0.0   # rate-limit waiting is budgeted per call, not per attempt
        for attempt in range(self.s.max_retries + 1):
            started = time.perf_counter()
            try:
                data = self._post(body)
                msg = data["choices"][0]["message"]
                msg = {"role": "assistant", "content": msg.get("content") or "",
                       **({"tool_calls": msg["tool_calls"]} if msg.get("tool_calls") else {})}
                recovered = False
                if not msg.get("tool_calls") and known:
                    salvaged = parse_text_tool_calls(msg["content"], known)
                    if salvaged:
                        msg = {"role": "assistant", "content": "", "tool_calls": _as_tool_calls(salvaged)}
                        recovered = True
                return LLMResponse(msg, data.get("usage", {}), (time.perf_counter() - started) * 1000, recovered)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                # Groq returns 400 tool_use_failed when the model emits a malformed call:
                # salvage the attempted call from `failed_generation` instead of failing the turn.
                optional = [k for k in ("parallel_tool_calls", "reasoning_effort") if k in body and k in detail]
                if exc.code == 400 and optional:
                    # Some models reject optional parameters: drop them and retry immediately.
                    for k in optional:
                        body.pop(k)
                    last_err = LLMError(f"Model rejected optional parameter(s) {optional}: {detail[:200]}")
                    continue
                if exc.code == 400 and "tool_use_failed" in detail:
                    try:
                        failed = json.loads(detail)["error"].get("failed_generation", "")
                    except (json.JSONDecodeError, KeyError, AttributeError):
                        failed = ""
                    salvaged = parse_text_tool_calls(failed, known)
                    if salvaged:
                        return LLMResponse({"role": "assistant", "content": "", "tool_calls": _as_tool_calls(salvaged)},
                                           {}, (time.perf_counter() - started) * 1000, True)
                    last_err = LLMError(f"Model produced an invalid tool call (HTTP 400 tool_use_failed): {failed[:200]}")
                    body["temperature"] = min(0.7, body["temperature"] + 0.2)   # nudge out of the bad mode
                elif exc.code == 429:
                    # Free tiers cap tokens per minute. Wait as long as the provider asks (bounded),
                    # rather than failing the guest's turn.
                    wait = _retry_after(exc, detail)
                    scope = "per day" if "per day" in detail or "(TPD)" in detail else "per minute"
                    last_err = LLMError(f"Rate limit reached (HTTP 429, tokens {scope}). The provider asked to "
                                        f"wait {wait:.0f}s. {detail[:160]}")
                    if attempt < self.s.max_retries and waited + wait <= MAX_RATE_LIMIT_WAIT_S:
                        if self.on_wait:
                            self.on_wait(wait)
                        time.sleep(wait + 0.5)
                        waited += wait + 0.5
                        continue
                    # The provider wants a longer wait than this turn can afford: fail fast with a
                    # clear message instead of burning retries (and the guest's time) on backoff.
                    raise last_err
                elif exc.code in (500, 502, 503, 504):
                    last_err = LLMError(f"LLM provider error (HTTP {exc.code}): {detail[:200]}")
                elif exc.code == 401:
                    raise LLMError("LLM API key rejected (HTTP 401). Check LLM_API_KEY in .env.") from None
                elif exc.code == 403:
                    raise LLMError(f"Request blocked (HTTP 403) - key not allowed for this model, or blocked by the "
                                   f"provider's firewall/VPN: {detail[:200]}") from None
                elif exc.code == 404:
                    raise LLMError(f"Model or URL not found (HTTP 404). Check LLM_MODEL and LLM_BASE_URL: "
                                   f"{detail[:200]}") from None
                else:
                    raise LLMError(f"LLM request failed (HTTP {exc.code}): {detail[:300]}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                reason = getattr(exc, "reason", exc)
                hint = (" (SSL certificate problem - often a corporate proxy/antivirus; try another network)"
                        if "CERTIFICATE" in str(reason).upper() else "")
                last_err = LLMError(f"Could not reach {self.s.base_url}: {reason}{hint}")
            except (KeyError, IndexError, json.JSONDecodeError) as exc:
                last_err = LLMError(f"Malformed LLM response: {exc}")
            if attempt < self.s.max_retries:
                time.sleep(min(8.0, 0.6 * 2 ** attempt + random.random() * 0.3))   # exp. backoff + jitter
        raise last_err or LLMError("LLM call failed")

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.s.base_url}/chat/completions", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.s.api_key}",
                     "User-Agent": "goodfoods-agent/1.0"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.s.timeout_s) as resp:
            return json.loads(resp.read().decode())
