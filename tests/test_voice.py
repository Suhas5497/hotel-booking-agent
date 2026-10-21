"""Voice: transcription client (with a fake HTTP server) and text prepared for speaking."""
import io
import json
import unittest
import urllib.error
from unittest import mock

from goodfoods import voice
from goodfoods.voice import STTSettings, VoiceError

SETTINGS = STTSettings(base_url="https://api.example.test/openai/v1", api_key="k-123", model="whisper-large-v3-turbo")


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code):
    return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(b"{}"))


class Transcribe(unittest.TestCase):
    def test_sends_audio_to_whisper_and_returns_text(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"], seen["headers"], seen["body"] = req.full_url, dict(req.header_items()), req.data
            return FakeResponse(json.dumps({"text": "  Tomorrow is my anniversary,\n table for two  "}).encode())

        with mock.patch.object(voice.urllib.request, "urlopen", fake_urlopen):
            text = voice.transcribe(b"RIFFfakewav", SETTINGS)
        self.assertEqual(text, "Tomorrow is my anniversary, table for two")
        self.assertEqual(seen["url"], "https://api.example.test/openai/v1/audio/transcriptions")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer k-123")
        self.assertIn("multipart/form-data; boundary=", seen["headers"]["Content-type"])
        body = seen["body"]
        self.assertIn(b'name="model"\r\n\r\nwhisper-large-v3-turbo', body)
        self.assertIn(b'name="file"; filename="speech.wav"', body)
        self.assertIn(b"RIFFfakewav", body)
        self.assertIn(b"Koramangala", body, "the spelling hint should list areas")

    def test_errors_are_friendly(self):
        for code, words in ((401, "rejected"), (404, "isn't available"), (429, "in a minute"), (500, "HTTP 500")):
            with mock.patch.object(voice.urllib.request, "urlopen", side_effect=http_error(code)):
                with self.assertRaises(VoiceError) as ctx:
                    voice.transcribe(b"x", SETTINGS)
            self.assertIn(words, str(ctx.exception))

    def test_network_failure(self):
        with mock.patch.object(voice.urllib.request, "urlopen", side_effect=urllib.error.URLError("down")):
            with self.assertRaises(VoiceError):
                voice.transcribe(b"x", SETTINGS)

    def test_needs_a_key_unless_local(self):
        with self.assertRaises(VoiceError):
            voice.transcribe(b"x", STTSettings("https://api.groq.com/openai/v1", "", "m"))
        with mock.patch.object(voice.urllib.request, "urlopen",
                               return_value=FakeResponse(b'{"text": "hello there"}')):
            self.assertEqual(voice.transcribe(b"x", STTSettings("http://localhost:8000/v1", "", "m")), "hello there")

    def test_empty_or_noise_is_rejected(self):
        with self.assertRaises(VoiceError):
            voice.transcribe(b"", SETTINGS)
        for heard in ("", "Thank you.", "you", "[Music]"):
            with mock.patch.object(voice.urllib.request, "urlopen",
                                   return_value=FakeResponse(json.dumps({"text": heard}).encode())):
                with self.assertRaises(VoiceError, msg=heard):
                    voice.transcribe(b"x", SETTINGS)

    def test_settings_default_to_the_chat_provider(self):
        llm = mock.Mock(base_url="https://api.groq.com/openai/v1", api_key="gsk")
        with mock.patch.dict("os.environ", {}, clear=False):
            for k in ("STT_BASE_URL", "STT_API_KEY", "STT_MODEL"):
                __import__("os").environ.pop(k, None)
            s = voice.load_stt_settings(llm)
        self.assertEqual((s.base_url, s.api_key, s.model), (llm.base_url, "gsk", "whisper-large-v3-turbo"))


class Speakable(unittest.TestCase):
    def test_options_read_naturally(self):
        reply = ("Happy anniversary! Three places free at 8 PM on Saturday:\n"
                 "1. **Skyline Grill**, Electronic City · ₹1,760 · 4.7★ · rooftop\n"
                 "2. Sakura – HSR Layout · ₹1,580 · 4.9★ · quiet zone\nWhich one would you like?")
        out = voice.speakable(reply)
        self.assertIn("Option 1: Skyline Grill, Electronic City, 1,760 rupees, 4.7 stars", out)
        self.assertIn("Option 2: Sakura, HSR Layout, 1,580 rupees, 4.9 stars", out)
        self.assertTrue(out.endswith("Which one would you like?"))
        for sym in ("₹", "★", "*", "·", "\n"):
            self.assertNotIn(sym, out)

    def test_codes_and_phone_are_spelled(self):
        out = voice.speakable("Booked! Code GF-AB12CD for +91 ******3210.")
        self.assertIn("G F A B 1 2 C D", out)
        self.assertIn("the number ending 3 2 1 0", out)
        self.assertNotIn("******", out)

    def test_links_dropped(self):
        self.assertEqual(voice.speakable("Directions: [Open map](https://maps.example/x) or https://x.y/z"),
                         "Directions: Open map or.")

    def test_language(self):
        self.assertEqual(voice.speech_lang("कल रात 8 बजे"), "hi-IN")
        self.assertEqual(voice.speech_lang("ನಮಸ್ಕಾರ"), "kn-IN")
        self.assertEqual(voice.speech_lang("Kal raat 8 baje"), "en-IN")

    def test_script_is_safe_json(self):
        js = voice.speak_script('He said "</script><b>hi"', "en-IN")
        payload = json.loads(js.split("var p = ", 1)[1].split(";\n", 1)[0])
        self.assertEqual(payload["text"], 'He said "</script><b>hi"')
        self.assertEqual(js.count("</script>"), 1, "text must not be able to close the script tag")


if __name__ == "__main__":
    unittest.main()
