"""Voice for Gia: speech-to-text for the guest, text-to-speech for Gia's replies.

* Listening: the recording from the browser mic is sent to a Whisper model through any
  OpenAI-compatible `/audio/transcriptions` endpoint. By default that is the same Groq key
  the chat model uses (`whisper-large-v3-turbo`, which understands English and Hindi).
* Speaking: Gia's voice is Sarvam Bulbul (male voice "shubh" by default, set SARVAM_API_KEY).
  Without a key, or if Sarvam fails, the guest's browser speaks instead (Web Speech API, free).
  Text is cleaned first so "₹1,580 · 4.9★" is read as "1,580 rupees, 4.9 stars".

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


# ----------------------------------------------------------------------------- Gia's voice (Sarvam Bulbul)

@dataclass(frozen=True)
class TTSSettings:
    api_key: str
    model: str = "bulbul:v3"
    speaker: str = "shubh"          # a male Bulbul v3 voice; others: aditya, kabir, rohan, rahul, amit...
    pace: float = 1.1               # a touch quicker than normal sounds brighter
    temperature: float = 0.75       # higher = more expressive (Sarvam's range 0.01-1.0, default 0.6)
    base_url: str = "https://api.sarvam.ai"
    timeout_s: float = 20.0


def load_tts_settings() -> TTSSettings:
    """Gia's voice. Without SARVAM_API_KEY she uses the browser's built-in voice instead."""
    def num(name, default, lo, hi):
        try:
            return min(hi, max(lo, float(os.getenv(name, default))))
        except ValueError:
            return float(default)
    return TTSSettings(
        api_key=os.getenv("SARVAM_API_KEY", "").strip(),
        model=os.getenv("SARVAM_TTS_MODEL", "bulbul:v3").strip(),
        speaker=os.getenv("SARVAM_SPEAKER", "shubh").strip().lower(),
        pace=num("SARVAM_PACE", "1.1", 0.5, 2.0),
        temperature=num("SARVAM_TEMPERATURE", "0.75", 0.01, 1.0),
        base_url=os.getenv("SARVAM_BASE_URL", "https://api.sarvam.ai").rstrip("/"),
    )


SARVAM_LANGS = {"hi-IN", "en-IN", "kn-IN", "bn-IN", "gu-IN", "ml-IN", "mr-IN", "od-IN", "pa-IN", "ta-IN", "te-IN"}
_TTS_CACHE: dict[tuple, bytes] = {}
_TTS_CACHE_MAX = 64


def synthesize(text: str, lang: str = "en-IN", settings: TTSSettings | None = None) -> bytes:
    """Gia's line as audio (MP3, or WAV if the service sends that), via Sarvam Bulbul.
    Repeated lines (the greeting, "Let me check…") come from a small in-memory cache.
    Raises VoiceError; callers fall back to the browser voice."""
    s = settings or load_tts_settings()
    if not s.api_key:
        raise VoiceError("No SARVAM_API_KEY set, so Gia uses the browser voice.")
    text = (text or "").strip()[:2500]
    if not text:
        raise VoiceError("Nothing to say.")
    lang = lang if lang in SARVAM_LANGS else "en-IN"
    key = (text, lang, s.model, s.speaker, s.pace, s.temperature)
    if key in _TTS_CACHE:
        return _TTS_CACHE[key]
    body = {"text": text, "language_code": lang, "model": s.model, "speaker": s.speaker, "pace": s.pace,
            "temperature": s.temperature, "speech_sample_rate": 24000, "output_audio_codec": "mp3"}
    req = urllib.request.Request(f"{s.base_url}/text-to-speech", data=json.dumps(body).encode(), method="POST",
                                 headers={"api-subscription-key": s.api_key, "Content-Type": "application/json",
                                          "User-Agent": "goodfoods-agent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=s.timeout_s) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        if exc.code in (401, 403):
            raise VoiceError("Sarvam rejected the API key. Check SARVAM_API_KEY in .env.") from None
        if exc.code == 429:
            raise VoiceError("Sarvam voice quota or rate limit reached; using the browser voice for now.") from None
        if exc.code in (400, 422):
            raise VoiceError(f"Sarvam couldn't voice that line (check SARVAM_SPEAKER / SARVAM_TTS_MODEL): {detail}") from None
        raise VoiceError(f"Sarvam voice failed (HTTP {exc.code}).") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        raise VoiceError("Couldn't reach Sarvam for Gia's voice.") from None
    except json.JSONDecodeError:
        raise VoiceError("Sarvam sent an unexpected reply.") from None
    try:
        import base64
        audio = b"".join(base64.b64decode(a) for a in data.get("audios") or [])
    except (ValueError, TypeError):
        raise VoiceError("Sarvam sent audio that couldn't be read.") from None
    if not audio:
        raise VoiceError("Sarvam sent no audio.")
    if len(_TTS_CACHE) >= _TTS_CACHE_MAX:
        _TTS_CACHE.pop(next(iter(_TTS_CACHE)))
    _TTS_CACHE[key] = audio
    return audio


def audio_mime(audio: bytes) -> str:
    """The audio's real type, whatever was asked for."""
    if audio[:4] == b"RIFF":
        return "audio/wav"
    if audio[:4] == b"fLaC":
        return "audio/flac"
    return "audio/mpeg"


def speak_script(text: str | None, lang: str = "en-IN", rate: float = 1.0, audio: bytes | None = None) -> str:
    """HTML/JS for a zero-height Streamlit component that speaks `text` (or, with None, stops).

    With `audio` (Gia's Sarvam voice) it plays that clip; otherwise, or if the browser refuses to
    play it, it falls back to the browser's own speech engine. Both run in the *parent* page:
    browsers only allow sound after the guest has interacted with the page, and a fresh
    component iframe hasn't been, but the page around it has. Gia's stage gets the
    `gia-talking` class while she speaks, so she bounces in time."""
    import base64
    src = f"data:{audio_mime(audio)};base64,{base64.b64encode(audio).decode()}" if audio else ""
    payload = json.dumps({"text": text or "", "lang": lang, "rate": rate, "audio": src, "nonce": secrets.token_hex(4)})
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e")   # text can't close the <script>
    return """<script>
(function () {
  var p = %s;
  var W = window;
  try { if (window.parent && window.parent.document) W = window.parent; } catch (e) {}
  // Gia's stage bounces while she speaks (class read by goodfoods/mascot.py's CSS).
  function talking(on) {
    try {
      var stages = W.document.querySelectorAll(".gia-stage");
      for (var i = 0; i < stages.length; i++) stages[i].classList.toggle("gia-talking", on);
    } catch (e) {}
  }
  var synth = W.speechSynthesis || window.speechSynthesis || null;
  // Stop whatever Gia was saying before.
  try { if (W.__giaAudio) { W.__giaAudio.pause(); W.__giaAudio = null; } } catch (e) {}
  try { if (synth) synth.cancel(); } catch (e) {}
  talking(false);
  if (!p.text && !p.audio) return;

  function browserVoice() {
    if (!synth || !p.text) return;
    var Utter = W.SpeechSynthesisUtterance || window.SpeechSynthesisUtterance;
    if (!Utter) return;
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
  }

  if (p.audio) {
    try {
      var A = W.Audio || window.Audio;
      var a = new A(p.audio);
      W.__giaAudio = a;
      a.onplaying = function () { talking(true); };
      a.onended = function () { talking(false); if (W.__giaAudio === a) W.__giaAudio = null; };
      a.onpause = function () { talking(false); };
      a.onerror = function () { talking(false); browserVoice(); };
      var started = a.play();
      if (started && started.catch) started.catch(function () { talking(false); browserVoice(); });
      return;
    } catch (e) { /* fall through to the browser voice */ }
  }
  browserVoice();
})();
</script>""" % payload
