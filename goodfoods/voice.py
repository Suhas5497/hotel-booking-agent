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


# One-word answers that are real replies, not noise.
_SHORT_OK = {"yes", "yeah", "yep", "yup", "no", "nope", "ok", "okay", "sure", "confirm", "cancel", "book", "done",
             "haan", "ha", "han", "nahi", "na", "ji", "theek", "thik", "stop", "help", "cheaper", "thanks", "first",
             "second", "third", "today", "tomorrow", "kal", "parso", "tonight", "lunch", "dinner", "breakfast",
             "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
             "ek", "do", "teen", "char", "chaar", "paanch", "chhe", "saat", "aath", "nau", "das"}


def is_unclear(text: str) -> bool:
    """True for recordings that are almost certainly not a real request: a single word that isn't a
    known short answer, number, day, area or outlet, or a few words in another script/accents
    (Whisper hearing noise or Gia's own voice, e.g. "née"). The guest is asked to say it again."""
    words = re.findall(r"[^\W_]+", (text or "").lower())
    if not words:
        return True
    if re.search(r"[à-öø-ÿœ]", text.lower()) and len(words) <= 3:
        return True   # accented Latin letters in a short clip: French/Spanish guesses on noise
    if len(words) > 1:
        return False
    w = words[0]
    if w in _SHORT_OK or w.isdigit() or re.fullmatch(r"[\u0900-\u097f]+", w):
        return False
    from .seed import CONCEPTS
    known = {a.lower() for a in catalog.AREAS} | {c[0].lower().split()[0] for c in CONCEPTS} | \
        {d.lower() for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")}
    return w not in known


def _is_noise(text: str) -> bool:
    return text.strip().lower().strip(".!? ") in {n.strip(".!? ") for n in _NOISE}


# ----------------------------------------------------------------------------- speaking

def _spell_code(m: re.Match) -> str:
    return "G F " + " ".join(m.group(1))


_WEEKDAYS = {"Mon": "Monday", "Tue": "Tuesday", "Tues": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday",
             "Thur": "Thursday", "Thurs": "Thursday", "Fri": "Friday", "Sat": "Saturday", "Sun": "Sunday"}
_MONTHS = {"Jan": "January", "Feb": "February", "Mar": "March", "Apr": "April", "Jun": "June", "Jul": "July",
           "Aug": "August", "Sep": "September", "Sept": "September", "Oct": "October", "Nov": "November", "Dec": "December"}
_ORDINAL_OPENERS = {1: "The first one is", 2: "The second is", 3: "And the third is", 4: "The fourth is", 5: "The fifth is"}
_FEATURE_WORDS = {"bar": "a bar", "rooftop": "a rooftop", "quiet zone": "a quiet zone", "quiet": "a quiet setting",
                  "wifi": "Wi-Fi", "wi-fi": "Wi-Fi", "kid-friendly": "a kid-friendly space", "kid friendly": "a kid-friendly space",
                  "pet-friendly": "a pet-friendly space", "pet friendly": "a pet-friendly space",
                  "pure veg": "a pure veg menu", "veg": "a pure veg menu", "bbq": "barbecue", "bbq platter": "a barbecue platter",
                  "wheelchair-accessible": "wheelchair access", "wheelchair accessible": "wheelchair access",
                  "valet parking": "valet parking", "valet": "valet parking"}
_SKIP_FEATURES = {"top-rated", "top rated", "highly rated", "popular"}   # already said by the rating


def _join_and(items: list[str]) -> str:
    items = [i for i in items if i]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


def _say_dates_and_times(s: str, today=None) -> str:
    """"2026-10-17" -> "Saturday 17 October"; "Sat, 17 Oct 2026" -> "Saturday, 17 October";
    "20:00" -> "8 PM"; "8:00 PM" -> "8 PM". The year is dropped when it's this year."""
    from datetime import date as _date
    from .config import now as _now
    year = (today or _now().date()).year

    def iso(m):
        try:
            d = _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return m.group(0)
        return d.strftime("%A ") + str(d.day) + d.strftime(" %B") + ("" if d.year == year else f" {d.year}")
    s = re.sub(r"\b(20\d\d)-(\d\d)-(\d\d)\b", iso, s)
    s = re.sub(r"\b(Mon|Tues?|Wed|Thu(?:rs?)?|Fri|Sat|Sun)\b(?=,?\s*\d)", lambda m: _WEEKDAYS[m.group(1)], s)
    s = re.sub(r"(?<=\bon\s)(Mon|Tues?|Wed|Thu(?:rs?)?|Fri|Sat|Sun)\b", lambda m: _WEEKDAYS[m.group(1)], s)
    s = re.sub(r"(?<=\d\s)(Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\b", lambda m: _MONTHS[m.group(1)], s)
    s = re.sub(r"\b(Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\b(?=\s+\d)", lambda m: _MONTHS[m.group(1)], s)
    s = re.sub(r"(\d{1,2} [A-Z][a-z]+),? " + str(year) + r"\b", r"\1", s)

    def hhmm(m):
        h, mm, ampm = int(m.group(1)), m.group(2), (m.group(3) or "").upper().replace(".", "")
        if not ampm:
            if h > 23:
                return m.group(0)
            ampm = "PM" if h >= 12 else "AM"
            h = h % 12 or 12
        return f"{h} {ampm}" if mm == "00" else f"{h}:{mm} {ampm}"
    s = re.sub(r"\b([01]?\d|2[0-3]):([0-5]\d)(?:\s*([AaPp]\.?[Mm]\.?))?(?!\d)", hhmm, s)
    return s


def _natural_list_item(n: int, item: str) -> str:
    """"Skyline Grill – Indiranagar · ₹1,760/person · 4.7★ · bar, live music"
    -> "The first one is Skyline Grill in Indiranagar. It's rated 4.7, about 1,760 rupees per person,
       and has a bar and live music." Falls back to "The first one is <item>" for other lists."""
    opener = _ORDINAL_OPENERS.get(n, f"Number {n} is")
    raw = re.sub(r"[*_`]+", "", item).strip().rstrip(".")
    parts = [x.strip() for x in re.split(r"\s*[·|]\s*|\s+[–—-]\s+|,\s+|:\s+", raw) if x.strip()]
    if not parts:
        return f"{opener} {item}"
    name = re.sub(r"^GoodFoods\s+", "", parts[0])
    area = price = rating = None
    features: list[str] = []
    areas = {a.lower(): a for a in catalog.AREAS}
    for part in parts[1:]:
        low = part.lower()
        pm = re.search(r"₹\s?([\d,]+)", part) or re.search(r"([\d,]+)\s*rupees", low)
        rm = re.search(r"(\d(?:\.\d)?)\s*(?:★|stars?)", part) or re.search(r"rated\s+(\d(?:\.\d)?)", low)
        if pm and not price:
            price = pm.group(1)
        elif rm and not rating:
            rating = rm.group(1)
        elif low in areas and not area:
            area = areas[low]
        elif low.startswith("in ") and low[3:] in areas and not area:
            area = areas[low[3:]]
        else:
            features.append(_FEATURE_WORDS.get(low, re.sub(r"\bbbq\b", "barbecue", part, flags=re.I)))
    if not (price or rating or area):
        return f"{opener} {item.strip()}"
    where = (f" on {area}" if area.endswith("Road") else f" in {area}") if area else ""
    first = f"{opener} {name}{where}."
    if price and rating:
        facts = f"It's {price} rupees per person, with a {rating} star rating"
    elif price:
        facts = f"It's {price} rupees per person"
    elif rating:
        facts = f"It has a {rating} star rating"
    else:
        facts = ""
    features = [f for f in features if f.lower() not in _SKIP_FEATURES]
    if features:
        facts = (facts + ", and it has " if facts else "It has ") + _join_and(features)
    return first + (" " + facts + "." if facts else "")


def _natural_lists(s: str) -> str:
    """Option lists, numbered ("1.") or bulleted ("*", "-", "•"), become spoken sentences:
    "The first one is…", "The second is…". Bullets are counted in order within each list."""
    out, bullet_n = [], 0
    for line in s.split("\n"):
        m = re.match(r"^\s*(\d+)[.)]\s+(.+)$", line)
        b = re.match(r"^\s*[*\-•]\s+(.+)$", line)
        if m:
            out.append(_natural_list_item(int(m.group(1)), m.group(2)))
            bullet_n = 0
        elif b:
            bullet_n += 1
            out.append(_natural_list_item(bullet_n, b.group(1)))
        else:
            if line.strip():
                bullet_n = 0
            out.append(line)
    return "\n".join(out)


def speakable(text: str) -> str:
    """The reply as Gia will say it: the same words, in the same order, with what a voice can't
    read said naturally: option lists become "The first one is…", short days and months are
    said in full, dates and times are said the way people say them, ₹ and ★ become rupees and
    stars, booking codes are spelled out, masked phone numbers become "the number ending…"."""
    s = str(text or "")
    s = s.replace("\u2011", "-").replace("\u2010", "-").replace("\u00a0", " ").replace("\u202f", " ")   # non-breaking hyphens/spaces
    s = re.sub(r"```.*?```", " ", s, flags=re.S)                      # code blocks
    s = _natural_lists(s)                                              # "1. X · ₹…" -> "The first one is X…"
    s = _say_dates_and_times(s)                                        # Sat -> Saturday, 2026-10-17, 20:00
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)                     # [label](url) -> label
    s = re.sub(r"https?://\S+", " ", s)                                # bare links
    s = re.sub(r"\+91\s*\**\s*(\d{4})\b", lambda m: "the number ending " + " ".join(m.group(1)), s)
    s = re.sub(r"\bGF-([A-Z0-9]{6})\b", _spell_code, s)                # read codes letter by letter
    s = re.sub(r"\bGF-\*+([A-Z0-9]{2})\b", lambda m: "the code ending " + " ".join(m.group(1)), s)
    s = re.sub(r"₹\s?([\d,]+)", r"\1 rupees", s)
    s = re.sub(r"\s*/\s*(person|head|night|guest)\b", r" per \1", s)  # "₹720/person" -> "720 rupees per person"
    s = re.sub(r"(\d(?:\.\d)?)\s?★", r"\1 stars", s)
    s = s.replace("★", " stars").replace("₹", " rupees ")
    s = re.sub(r"(?m)^\s*[-*•]\s+", "", s)                             # bullet marks
    s = re.sub(r"[*_`#>|]+", "", s)                                    # markdown emphasis, headings
    s = s.replace("·", ",").replace("→", " to ").replace(" – ", ", ").replace(" — ", ", ").replace("–", " ").replace("—", ", ")
    s = re.sub(r"(?m)^\s*(\d+)[.)]\s+", r"\1. ", s)                    # keep list numbers as written
    s = re.sub(r"([^.!?:;,\s])\s*\n+\s*", r"\1. ", s)                  # a line break ends a sentence
    s = re.sub(r"\s*\n+\s*", " ", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\s+([,.!?])", r"\1", s)
    s = re.sub(r"([,!?])[,.]+", r"\1", s)
    s = re.sub(r"\.{2,}(?!\.)", ".", s.replace("…", "…"))
    s = s.strip(" ,")[:3000]
    return s if not s or s.endswith(("?", "!", ".", "…")) else s + "."


_ABBREV = {"co", "dr", "mr", "mrs", "ms", "st", "rd", "no", "vs", "approx", "etc", "jr", "sr"}


def split_for_speech(text: str, max_len: int = 220) -> list[str]:
    """Sentences to speak one after another. Short pieces stay together; very long sentences are
    split at commas. List numbers ("1.") and abbreviations ("Co.") never end a sentence.
    Speaking in pieces stops browsers from cutting long messages off part-way."""
    text = (text or "").strip()
    if not text:
        return []
    pieces, start = [], 0
    for m in re.finditer(r"[.!?…]+[\"”')]*\s+", text):
        before = text[start:m.start()].split()
        last = before[-1].lower().strip("(\"'") if before else ""
        list_number = last.isdigit() and len(last) <= 2 and (len(before) == 1 or before[-2].endswith(":"))
        if list_number or last in _ABBREV or re.fullmatch(r"[a-z]", last):
            continue
        pieces.append(text[start:m.end()].strip())
        start = m.end()
    if text[start:].strip():
        pieces.append(text[start:].strip())
    out: list[str] = []
    for piece in pieces:   # break any run-on sentence at commas
        while len(piece) > max_len:
            cut = piece.rfind(", ", 0, max_len)
            cut = cut + 1 if cut > 40 else max_len
            out.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            out.append(piece)
    merged: list[str] = []   # glue tiny fragments ("Sure!") to the next sentence
    for piece in out:
        if merged and len(merged[-1]) < 25 and len(merged[-1]) + len(piece) < max_len:
            merged[-1] = merged[-1] + " " + piece
        else:
            merged.append(piece)
    return merged


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
    pace: float = 1.0               # natural speed
    temperature: float = 0.65       # a little livelier than Sarvam's default 0.6 without sounding forced
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
        pace=num("SARVAM_PACE", "1.0", 0.5, 2.0),
        temperature=num("SARVAM_TEMPERATURE", "0.65", 0.01, 1.0),
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
        raw = exc.read().decode("utf-8", "replace")
        try:   # Sarvam errors look like {"error": {"message": ..., "code": ...}}
            err = json.loads(raw).get("error") or {}
            detail = str(err.get("message") or err.get("code") or raw)[:160]
        except (json.JSONDecodeError, AttributeError):
            detail = raw[:160]
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


def synthesize_lines(lines: list[str], lang: str, settings: TTSSettings | None = None) -> list[bytes] | None:
    """Sarvam audio for every sentence, fetched in parallel so Gia starts speaking quickly.
    None if any sentence fails: then the whole reply uses the browser voice, never a mix."""
    s = settings or load_tts_settings()
    if not s.api_key or not lines:
        return None
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(4, len(lines))) as pool:
        futures = [pool.submit(synthesize, line, lang, s) for line in lines]
        try:
            return [f.result() for f in futures]
        except VoiceError:
            raise


def group_lines(lines: list[str], max_chars: int = 2000) -> list[tuple[int, int]]:
    """Consecutive sentences joined into as few pieces as fit Sarvam's limit, as (start, end) ranges.
    One clip per reply keeps Gia's tone flowing naturally instead of resetting every sentence."""
    groups, start, size = [], 0, 0
    for i, line in enumerate(lines):
        if i > start and size + len(line) + 1 > max_chars:
            groups.append((start, i))
            start, size = i, 0
        size += len(line) + 1
    if lines:
        groups.append((start, len(lines)))
    return groups


def synthesize_groups(lines: list[str], lang: str, settings: TTSSettings | None = None) -> list[tuple[bytes, int, int]] | None:
    """Sarvam clips for the reply: usually one clip for the whole reply. None without a key;
    raises VoiceError if any piece fails, so the caller uses the browser voice for all of it."""
    s = settings or load_tts_settings()
    if not s.api_key or not lines:
        return None
    groups = group_lines(lines)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(4, len(groups))) as pool:
        futures = [pool.submit(synthesize, " ".join(lines[a:b]), lang, s) for a, b in groups]
        return [(f.result(), a, b) for f, (a, b) in zip(futures, groups)]


def sounds_like_echo(heard: str, said: str) -> bool:
    """True if what the mic heard is mostly Gia's own last reply (the mic picked up her voice)."""
    import difflib
    words = lambda t: re.findall(r"[a-z0-9ऀ-ॿ]+", (t or "").lower())
    h, g = words(heard), words(speakable(said)) + words(said)
    if len(h) < 5 or not g:
        return False
    gset = set(g)
    coverage = sum(1 for w in h if w in gset) / len(h)
    return coverage >= 0.8 or difflib.SequenceMatcher(None, h, words(speakable(said))).ratio() >= 0.6


# The speech controller runs in the main page, not in the component's helper frame: Streamlit
# deletes that frame on the next screen update, and anything living in it (timers, the
# "stop" function, the mic listener) would die with it. The frame only installs the
# controller once (or upgrades it) and hands it the reply.
CONTROLLER_VERSION = "3"
_CONTROLLER_JS = r"""
function giaInstall(W) {
  var D = W.document;
  var state = {stopped: true, audio: null, raf: null, timers: []};
  function talking(on) {   // Gia's stage bounces while she speaks (CSS in goodfoods/mascot.py)
    var stages = D.querySelectorAll(".gia-stage");
    for (var i = 0; i < stages.length; i++) stages[i].classList.toggle("gia-talking", on);
  }
  function subtitle(text) {  // the bubble shows the sentence being spoken
    var spans = D.querySelectorAll(".gia-stage .gia-line");
    for (var i = 0; i < spans.length; i++) if (spans[i].textContent !== text) spans[i].textContent = text;
  }
  function later(fn, ms) { var t = W.setTimeout(fn, ms); state.timers.push(t); return t; }
  function stop() {
    state.stopped = true;
    try { if (state.audio) state.audio.pause(); } catch (e) {}
    state.audio = null;
    try { if (W.speechSynthesis) W.speechSynthesis.cancel(); } catch (e) {}
    if (state.raf) { W.cancelAnimationFrame(state.raf); state.raf = null; }
    state.timers.forEach(function (t) { W.clearTimeout(t); }); state.timers = [];
    talking(false);
  }
  // Tapping the mic, or typing to Gia (a real tap or key press, never a focus change), stops her at once: she never talks over the guest
  // or into the mic. Several selectors, so a Streamlit update to one name doesn't break it.
  var GUEST_INPUT = '[data-testid="stAudioInput"], [data-testid*="AudioInput"], [class*="st-key-mic_"], ' +
                    '[data-testid="stChatInput"], [data-testid*="ChatInput"], .stChatInput';
  function interrupt(e) {
    var t = e.target;
    if (!state.stopped && t && t.closest && t.closest(GUEST_INPUT)) stop();
  }
  D.addEventListener("pointerdown", interrupt, true);
  D.addEventListener("keydown", interrupt, true);

  function speak(p) {
    stop();
    var lines = (p.lines || []).map(function (l) { return l.text; });
    if (!lines.length) return;
    state.stopped = false;
    var synth = W.speechSynthesis || null;
    var clips = (p.clips || []).map(function (c) { var a = new W.Audio(c.audio); a.preload = "auto"; return {a: a, from: c.from, to: c.to}; });
    function done() { if (state.raf) { W.cancelAnimationFrame(state.raf); state.raf = null; } talking(false); state.stopped = true; }

    // browser voice: one sentence at a time (long text in one go gets cut off in Chrome)
    function pickVoice() {
      var voices = (synth && synth.getVoices()) || [];
      var lang = p.lang.toLowerCase(), base = lang.split("-")[0];
      return voices.find(function (v) { return v.lang && v.lang.toLowerCase() === lang; })
          || voices.find(function (v) { return v.lang && v.lang.toLowerCase().indexOf(base) === 0; }) || null;
    }
    function browserFrom(i) {
      if (state.stopped) return;
      if (i >= lines.length || !synth || !W.SpeechSynthesisUtterance) { done(); return; }
      subtitle(lines[i]); talking(true);
      var u = new W.SpeechSynthesisUtterance(lines[i]);
      u.lang = p.lang; u.rate = p.rate;
      try { var v = pickVoice(); if (v) u.voice = v; } catch (e) {}
      u.onend = function () { later(function () { browserFrom(i + 1); }, 60); };
      u.onerror = function () { later(function () { browserFrom(i + 1); }, 60); };
      synth.speak(u);
    }
    function startBrowser(i) {
      if (!synth || (synth.getVoices() || []).length) { later(function () { browserFrom(i); }, 60); return; }
      var begun = false, go = function () { if (!begun) { begun = true; browserFrom(i); } };
      try { synth.addEventListener("voiceschanged", go); } catch (e) {}
      later(go, 700);
    }
    // Sarvam clips: subtitles follow the playback position, weighted by sentence length
    function follow(c) {
      var weights = [], total = 0;
      for (var i = c.from; i < c.to; i++) { total += lines[i].length + 8; weights.push(total); }
      (function tick() {
        if (state.stopped || state.audio !== c.a) return;
        var d = c.a.duration, t = c.a.currentTime;
        if (d && isFinite(d) && d > 0) {
          var at = (t / d) * total, k = 0;
          while (k < weights.length - 1 && at > weights[k]) k++;
          subtitle(lines[c.from + k]);
        }
        state.raf = W.requestAnimationFrame(tick);
      })();
    }
    function playClip(k) {
      if (state.stopped) return;
      if (k >= clips.length) { done(); return; }
      var c = clips[k];
      state.audio = c.a;
      subtitle(lines[c.from]);
      c.a.onplaying = function () { if (!state.stopped) { talking(true); follow(c); } };
      c.a.onended = function () { later(function () { playClip(k + 1); }, 120); };
      c.a.onerror = function () { if (!state.stopped) startBrowser(c.from); };
      var started = c.a.play();
      if (started && started.catch) started.catch(function () { if (!state.stopped) startBrowser(c.from); });
    }
    if (clips.length) later(function () { playClip(0); }, 40); else startBrowser(0);
  }
  W.GiaVoice = {v: "%(version)s", speak: speak, stop: stop, state: state,
                remove: function () { stop(); D.removeEventListener("pointerdown", interrupt, true);
                                      D.removeEventListener("keydown", interrupt, true); }};
}
""" % {"version": CONTROLLER_VERSION}


def speak_script(lines: list[str] | str | None, lang: str = "en-IN", rate: float = 1.0,
                 audio: list[bytes] | bytes | None = None,
                 clips: list[tuple[bytes, int, int]] | None = None) -> str:
    """HTML/JS for a zero-height Streamlit component that speaks Gia's reply (or, with None, stops).

    `lines` are the reply's sentences. `clips` are Sarvam audio clips, each covering sentences
    [start, end) (usually one clip for the whole reply); `audio` is the one-clip-per-line form.
    While a clip plays, Gia's bubble shows the sentence being spoken and her stage gets
    `gia-talking`. Without clips, or if the browser refuses to play them, the browser's own voice
    reads the same sentences one by one. Tapping the mic or typing to Gia stops her.

    The work is done by a controller installed once in the main page (see _CONTROLLER_JS), so it
    keeps running, and can still be stopped, after Streamlit removes this component's frame."""
    import base64
    if isinstance(lines, str):
        lines = [lines]
    lines = [l for l in (lines or []) if l and l.strip()]
    if isinstance(audio, (bytes, bytearray)):
        audio = [bytes(audio)]
    if clips is None and audio and len(audio) == len(lines):
        clips = [(a, i, i + 1) for i, a in enumerate(audio)]
    if clips and (clips[0][1] != 0 or clips[-1][2] != len(lines)):
        clips = None   # clips must cover the whole reply, or Gia would go quiet part-way
    clip_items = [{"audio": f"data:{audio_mime(c)};base64,{base64.b64encode(c).decode()}", "from": a, "to": b}
                  for c, a, b in (clips or [])]
    payload = json.dumps({"lines": [{"text": t} for t in lines], "clips": clip_items, "lang": lang, "rate": rate,
                          "nonce": secrets.token_hex(4)})
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e")   # text can't close the <script>
    controller = json.dumps(_CONTROLLER_JS).replace("<", "\\u003c").replace(">", "\\u003e")
    return """<script>
(function () {
  var p = %s;
  var SRC = %s, VERSION = "%s";
  var W = window;
  try { if (window.parent && window.parent.document) W = window.parent; } catch (e) {}
  if (!W.GiaVoice || W.GiaVoice.v !== VERSION) {
    try { if (W.GiaVoice && W.GiaVoice.remove) W.GiaVoice.remove(); } catch (e) {}
    try {   // install in the main page, so it outlives this frame
      var s = W.document.createElement("script");
      s.textContent = SRC + "\\ngiaInstall(window);";
      W.document.head.appendChild(s);
    } catch (e) {}
    if (!W.GiaVoice || W.GiaVoice.v !== VERSION) {   // the page refused the script: run it from here
      (new Function("W", SRC + "\\ngiaInstall(W);"))(W);
    }
  }
  W.GiaVoice.speak(p);
})();
</script>""" % (payload, controller, CONTROLLER_VERSION)
