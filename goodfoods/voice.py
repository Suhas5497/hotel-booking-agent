"""Voice for Gia: speech-to-text for the guest, text-to-speech for Gia's replies.

* Listening: the recording from the browser mic is sent to a Whisper model through any
  OpenAI-compatible `/audio/transcriptions` endpoint. By default that is the same Groq key
  the chat model uses (`whisper-large-v3-turbo`, which understands English and Hindi).
* Speaking: done in the guest's own browser with the Web Speech API (`speechSynthesis`),
  so it needs no key and costs nothing. Text is cleaned first so "₹1,580 · 4.9★" is read
  as "1,580 rupees, 4.9 stars" rather than symbol by symbol.

Standard library only, like the rest of the core.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import catalog
from .config import LLMSettings, load_llm_settings


class VoiceError(Exception):
    """A problem the guest can act on; the message is safe to show them."""


@dataclass(frozen=True)
class STTSettings:
    base_url: str
    api_key: str
    model: str
    timeout_s: float = 30.0


def load_stt_settings(llm: LLMSettings | None = None) -> STTSettings:
    """Speech-to-text settings. Defaults reuse the chat model's provider and key, so a Groq
    key is all that's needed. Set STT_BASE_URL / STT_API_KEY / STT_MODEL to use another one."""
    llm = llm or load_llm_settings()
    return STTSettings(
        base_url=os.getenv("STT_BASE_URL", llm.base_url).rstrip("/"),
        api_key=os.getenv("STT_API_KEY", llm.api_key),
        model=os.getenv("STT_MODEL", "whisper-large-v3-turbo"),
        timeout_s=float(os.getenv("STT_TIMEOUT_S", "30")),
    )


# Names Whisper would otherwise mishear ("Sakura" -> "Sacura", "Koramangala" -> "Cora Mangala").
# The prompt is only a spelling hint; it does not limit what the guest can say.
def _vocabulary_hint() -> str:
    from .seed import CONCEPTS   # (concept, cuisines, ...) tuples
    concepts = sorted({c[0] for c in CONCEPTS})
    words = ["GoodFoods", "Gia"] + concepts + list(catalog.AREAS) + ["biryani", "dosa", "anniversary", "table"]
    return ("Restaurant table booking in Bengaluru, English or Hindi. " + ", ".join(words))[:600]


def _multipart(fields: dict[str, str], file_field: str, filename: str, data: bytes,
               content_type: str) -> tuple[bytes, str]:
    boundary = "----gia" + secrets.token_hex(12)
    out = bytearray()
    for name, value in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n").encode()
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
            f"filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n").encode()
    out += data + b"\r\n" + f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


MAX_AUDIO_BYTES = 20 * 1024 * 1024   # well under provider limits; ~10 min of 16 kHz WAV


def transcribe(audio: bytes, settings: STTSettings | None = None, filename: str = "speech.wav",
               content_type: str = "audio/wav") -> str:
    """Turn a recording into text. Raises VoiceError with a guest-friendly message."""
    s = settings or load_stt_settings()
    if not audio:
        raise VoiceError("I didn't catch anything. Tap the mic and try again.")
    if len(audio) > MAX_AUDIO_BYTES:
        raise VoiceError("That recording is too long. Please keep it under a minute.")
    local = "localhost" in s.base_url or "127.0.0.1" in s.base_url
    if not s.api_key and not local:
        raise VoiceError("Voice input needs an API key. Set LLM_API_KEY (or STT_API_KEY) in .env.")
    body, ctype = _multipart({"model": s.model, "response_format": "json", "temperature": "0",
                              "prompt": _vocabulary_hint()}, "file", filename, audio, content_type)
    req = urllib.request.Request(f"{s.base_url}/audio/transcriptions", data=body, method="POST",
                                 headers={"Content-Type": ctype, "Authorization": f"Bearer {s.api_key}",
                                          "User-Agent": "goodfoods-agent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=s.timeout_s) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise VoiceError("Voice input: the API key was rejected. Check LLM_API_KEY / STT_API_KEY.") from None
        if exc.code == 404:
            raise VoiceError("Voice input isn't available with this model provider. Set STT_BASE_URL to "
                             "one with a Whisper model (Groq works), or type instead.") from None
        if exc.code == 429:
            raise VoiceError("Lots of requests right now. Please try the mic again in a minute, or type.") from None
        raise VoiceError(f"Voice input failed (HTTP {exc.code}). Please type instead.") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        raise VoiceError("Couldn't reach the voice service. Please check your connection, or type.") from None
    except json.JSONDecodeError:
        raise VoiceError("The voice service sent an unexpected reply. Please type instead.") from None
    text = re.sub(r"\s+", " ", str(data.get("text") or "")).strip()
    if not text or _is_noise(text):
        raise VoiceError("I didn't catch that. Tap the mic and speak a little closer.")
    return text[:1000]


# Whisper sometimes "hears" these in silence or background noise.
_NOISE = {"you", "thank you", "thanks for watching", "thank you for watching", "bye", ".", "...",
          "music", "[music]", "(music)", "silence", "[silence]"}


def _is_noise(text: str) -> bool:
    return text.strip().lower().strip(".!? ") in {n.strip(".!? ") for n in _NOISE}


# ----------------------------------------------------------------------------- speaking

def _spell_code(m: re.Match) -> str:
    return "G F " + " ".join(m.group(1))


def speakable(text: str) -> str:
    """Rewrite a chat reply so a speech engine reads it naturally."""
    s = str(text or "")
    s = re.sub(r"```.*?```", " ", s, flags=re.S)                      # code blocks
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)                     # [label](url) -> label
    s = re.sub(r"https?://\S+", " ", s)                                # bare links
    s = re.sub(r"\+91\s*\**\s*(\d{4})\b", lambda m: "the number ending " + " ".join(m.group(1)), s)
    s = re.sub(r"\bGF-([A-Z0-9]{6})\b", _spell_code, s)                # read codes letter by letter
    s = re.sub(r"₹\s?([\d,]+)", r"\1 rupees", s)
    s = re.sub(r"(\d(?:\.\d)?)\s?★", r"\1 stars", s)
    s = s.replace("★", " stars").replace("₹", " rupees ")
    s = re.sub(r"(?m)^\s*(\d+)[.)]\s+", r"Option \1: ", s)             # "1. Sakura" -> "Option 1: Sakura"
    s = re.sub(r"(?m)^\s*[-*•]\s+", "", s)                             # bullets
    s = re.sub(r"[*_`#>|]+", "", s)                                    # markdown emphasis, headings
    s = s.replace("·", ",").replace("→", " to ").replace("–", ", ").replace("—", ", ")
    s = re.sub(r"\s*\n+\s*", ". ", s)
    s = re.sub(r"([:;!?])\s*\.", r"\1", s)                             # "Saturday:." -> "Saturday:"
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\s+([,.!?])", r"\1", s)
    s = re.sub(r"([,.!?])[,.]+", r"\1", s)
    s = s.strip(" ,")[:1200]
    return s if not s or s.endswith(("?", "!", ".")) else s + "."


def speech_lang(text: str) -> str:
    """BCP-47 voice language: Hindi voice for Devanagari, Kannada for Kannada script,
    Indian English otherwise (also reads Latin-script Hinglish reasonably)."""
    if re.search(r"[ऀ-ॿ]", text or ""):
        return "hi-IN"
    if re.search(r"[ಀ-೿]", text or ""):
        return "kn-IN"
    return "en-IN"


def speak_script(text: str | None, lang: str = "en-IN", rate: float = 1.0) -> str:
    """HTML/JS for a zero-height Streamlit component that speaks `text` (or, with None, stops).

    It drives the *parent* page's speechSynthesis: browsers only allow speech after the guest
    has interacted with the page, and a freshly created component iframe has not been
    interacted with, but the page around it has. Falls back to the iframe's own engine."""
    payload = json.dumps({"text": text or "", "lang": lang, "rate": rate, "nonce": secrets.token_hex(4)})
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e")   # text can't close the <script>
    return """<script>
(function () {
  var p = %s;
  var synth = null;
  try { synth = window.parent.speechSynthesis; } catch (e) {}
  synth = synth || window.speechSynthesis;
  if (!synth || typeof SpeechSynthesisUtterance === "undefined") return;
  // Gia's stage bounces while she speaks (class read by goodfoods/mascot.py's CSS).
  function talking(on) {
    try {
      var stages = window.parent.document.querySelectorAll(".gia-stage");
      for (var i = 0; i < stages.length; i++) stages[i].classList.toggle("gia-talking", on);
    } catch (e) {}
  }
  synth.cancel();
  talking(false);
  if (!p.text) return;
  var Utter = (function () { try { return window.parent.SpeechSynthesisUtterance; } catch (e) { return null; } })()
              || SpeechSynthesisUtterance;
  function pickVoice() {
    var voices = synth.getVoices() || [];
    var lang = p.lang.toLowerCase(), base = lang.split("-")[0];
    return voices.find(function (v) { return v.lang && v.lang.toLowerCase() === lang; })
        || voices.find(function (v) { return v.lang && v.lang.toLowerCase().indexOf(base) === 0; })
        || null;
  }
  function go() {
    var u = new Utter(p.text);
    u.lang = p.lang; u.rate = p.rate;
    try { var v = pickVoice(); if (v) u.voice = v; } catch (e) {}   // no matching voice: browser default
    u.onstart = function () { talking(true); };
    u.onend = function () { talking(false); };
    u.onerror = function () { talking(false); };
    synth.speak(u);
  }
  if ((synth.getVoices() || []).length) { setTimeout(go, 60); }
  else {
    var done = false;
    var once = function () { if (!done) { done = true; go(); } };
    try { synth.addEventListener("voiceschanged", once); } catch (e) {}
    setTimeout(once, 700);
  }
})();
</script>""" % payload
