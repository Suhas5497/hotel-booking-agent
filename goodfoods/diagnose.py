"""Connection check for the LLM endpoint.   Run:  python -m goodfoods.diagnose

Runs four steps and stops at the first failure, printing the provider's own error:
  1. settings loaded from .env      2. model list (key valid? model name exists?)
  3. plain chat completion          4. a tool-calling request, then one real agent turn
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

from .config import ROOT, load_llm_settings


def _request(url: str, key: str, body: dict | None = None, timeout: float = 30) -> tuple[int, str, float]:
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                                          "User-Agent": "goodfoods-agent/1.0"},
                                 method="POST" if body else "GET")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), time.perf_counter() - started
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), time.perf_counter() - started


def _fail(step: str, why: str, fix: str) -> int:
    print(f"\n[FAIL] {step}\n  Why: {why}\n  Fix: {fix}")
    return 1


def main() -> int:
    s = load_llm_settings()
    print("1. Settings")
    print(f"   .env found     : {(ROOT / '.env').exists()}  ({ROOT / '.env'})")
    print(f"   LLM_BASE_URL   : {s.base_url}")
    print(f"   LLM_MODEL      : {s.model}")
    print(f"   LLM_API_KEY    : {'set, ' + str(len(s.api_key)) + ' chars, starts ' + s.api_key[:4] + '…' if s.api_key else 'MISSING'}")
    if not s.api_key and "localhost" not in s.base_url:
        return _fail("settings", "no API key", "put LLM_API_KEY=<your key> in .env (no quotes, no spaces)")
    if s.api_key.startswith(("your_", "<")):
        return _fail("settings", "the key is still the placeholder from .env.example", "paste your real Groq key")

    print("\n2. Model list")
    try:
        code, body, secs = _request(f"{s.base_url}/models", s.api_key)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return _fail("network", f"could not connect: {getattr(exc, 'reason', exc)}",
                     "check internet/VPN/proxy; if it mentions CERTIFICATE, an antivirus or office proxy is "
                     "intercepting HTTPS — try another network")
    print(f"   HTTP {code} in {secs:.1f}s")
    if code == 401:
        return _fail("auth", body[:300], "the key is wrong or revoked — create a new one at console.groq.com/keys")
    if code == 403:
        return _fail("blocked", body[:300], "the provider blocked the request (firewall/VPN/region) — try another network")
    if code == 200:
        ids = [m.get("id") for m in json.loads(body).get("data", [])]
        if s.model not in ids:
            skip = ("guard", "whisper", "tts", "playai", "orpheus", "embed", "safeguard")
            chat = [i for i in ids if not any(k in i.lower() for k in skip)][:12]
            return _fail("model", f"'{s.model}' is not offered by this endpoint",
                         f"set LLM_MODEL in .env to a chat model, e.g. one of: {chat}")
        print(f"   model '{s.model}' is available")

    print("\n3. Plain chat completion")
    code, body, secs = _request(f"{s.base_url}/chat/completions", s.api_key,
                                {"model": s.model, "messages": [{"role": "user", "content": "Reply with OK"}],
                                 "max_tokens": 5})
    print(f"   HTTP {code} in {secs:.1f}s")
    if code != 200:
        return _fail("chat", body[:400], "429 = rate limit (wait a minute); 400 = read the message above")

    print("\n4. Tool calling + one real agent turn")
    from . import build_stack
    agent, _ = build_stack(db_path=":memory:")
    started = time.perf_counter()
    turn = agent.chat("Show me Italian restaurants in Indiranagar")
    errors = [e.detail.get("message") for e in turn.trace if e.kind == "llm" and e.name == "error"]
    tools = [e.name for e in turn.trace if e.kind == "tool"]
    print(f"   {time.perf_counter() - started:.1f}s · tools called: {tools or 'none'}")
    if errors:
        return _fail("agent turn", errors[-1], "send this message to whoever is helping you debug")
    print(f"   Gia: {turn.reply[:300]}")
    if not tools:
        print("   [WARN] the model answered without calling a tool. Gia needs tool calling to check tables and\n"
              "          book; try another LLM_MODEL if this keeps happening.")

    print("\n5. Gia's voice (Sarvam Bulbul)")
    from . import voice
    tts = voice.load_tts_settings()
    if not tts.api_key:
        print("   SARVAM_API_KEY not set: Gia will use the browser's voice (free, varies by device).")
    else:
        try:
            started = time.perf_counter()
            audio = voice.synthesize("Hi! I'm Gia. What can I do for you today?", "en-IN", tts)
            print(f"   voice '{tts.speaker}' ({tts.model}) OK: {len(audio) // 1024} KB of audio in "
                  f"{time.perf_counter() - started:.1f}s")
        except voice.VoiceError as exc:
            return _fail("Sarvam voice", str(exc), "check SARVAM_API_KEY and SARVAM_SPEAKER in .env, or remove the key "
                         "to use the browser voice")
    print("\n[OK] Everything works. Start the app with:  python -m streamlit run app.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
