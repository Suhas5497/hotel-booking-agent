"""Hear Gia in several Sarvam voices, side by side, and pick the one you like.

    python -m goodfoods.voice_samples            # writes voice_samples.html, open it in your browser

Uses SARVAM_API_KEY from .env and your current SARVAM_PACE / SARVAM_TEMPERATURE. Each voice
reads the same short Gia lines in English and Hindi (roughly 2,000 characters in total, a
few rupees at most). The page shows the exact `.env` lines to switch to the voice you pick.
"""
from __future__ import annotations

import base64
import html
import sys
from dataclasses import replace
from pathlib import Path

from . import voice
from .config import ROOT

# Male voices worth comparing for a cheerful host. Bulbul v3 names come from Sarvam's voice list;
# the v4-flash entry is Sarvam's own default persona for that model (English/Hindi, ads style).
CANDIDATES = [
    ("bulbul:v3", "shubh", "Sarvam's top-rated male voice (current default)"),
    ("bulbul:v3", "aditya", ""),
    ("bulbul:v3", "kabir", ""),
    ("bulbul:v3", "rohan", ""),
    ("bulbul:v3", "rahul", ""),
    ("bulbul:v3", "amit", ""),
    ("bulbul:v3", "dev", ""),
    ("bulbul:v3", "advait", ""),
    ("bulbul:v4-flash", "shubh_enhi_ads", "newer model, upbeat 'ads' style"),
]
SAMPLE_EN = ("Good evening! I'm Gia from GoodFoods. Happy anniversary! I'd pick Sakura in HSR Layout: "
             "it's top rated and has a quiet zone, perfect for a surprise.")
SAMPLE_HI = "Namaste! Kal raat aath baje, do logon ke liye table book ho gaya hai. Enjoy kijiye!"


def main() -> int:
    base = voice.load_tts_settings()
    if not base.api_key:
        print("Set SARVAM_API_KEY in .env first.")
        return 1
    rows = []
    for model, speaker, note in CANDIDATES:
        s = replace(base, model=model, speaker=speaker)
        parts, results = [], []
        for label, text, lang in (("English", SAMPLE_EN, "en-IN"), ("Hinglish", SAMPLE_HI, "hi-IN")):
            try:
                audio = voice.synthesize(text, lang, s)
                parts.append(f'<p class="meta">{label}</p><audio controls preload="none" src="data:'
                             f'{voice.audio_mime(audio)};base64,{base64.b64encode(audio).decode()}"></audio>')
                results.append(f"{label} OK")
            except voice.VoiceError as exc:
                parts.append(f'<p class="meta">{label}</p><p class="err">{html.escape(str(exc))}</p>')
                results.append(f"{label} failed: {exc}")
        print(f"{speaker:16} {model:16} " + " · ".join(results))
        env = f"SARVAM_TTS_MODEL={model}<br>SARVAM_SPEAKER={speaker}"
        body = "".join(parts)
        rows.append(f'<section><h2>{html.escape(speaker)}</h2><p class="meta">{html.escape(model)}'
                    f'{" · " + html.escape(note) if note else ""}</p>{body}<code>{env}</code></section>')
    out = ROOT / "voice_samples.html"
    out.write_text(f"""<!doctype html><meta charset="utf-8"><title>Gia voice samples</title>
<style>
body {{ font-family: system-ui, sans-serif; background: #F4F5F2; color: #17201C; margin: 0; padding: 24px 16px; }}
main {{ max-width: 900px; margin: 0 auto; }}
h1 {{ font-size: 26px; margin: 0 0 4px; }} .lead {{ color: #5B6660; margin: 0 0 20px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }}
section {{ background: #fff; border: 1px solid #E3E6E1; border-radius: 14px; padding: 14px; display: grid; gap: 8px; }}
h2 {{ margin: 0; font-size: 18px; }} .meta {{ margin: 0; font-size: 13px; color: #5B6660; }}
audio {{ width: 100%; }} code {{ font-size: 12px; background: #F1F3EF; padding: 6px 8px; border-radius: 8px; }}
.err {{ color: #B42318; font-size: 13px; margin: 0; }}
</style>
<main><h1>Gia voice samples</h1>
<p class="lead">Each voice reads the same English line, then a Hinglish line. Pace {base.pace}, expressiveness
{base.temperature}. To use a voice, copy its two lines into your <b>.env</b> and restart the app.</p>
<div class="grid">{''.join(rows)}</div></main>""", encoding="utf-8")
    print(f"\nOpen {out} in your browser to listen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
