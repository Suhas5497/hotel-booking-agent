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
        self.assertIn("The first one is Skyline Grill in Electronic City. It's rated 4.7, about 1,760 rupees per person, "
                      "and has a rooftop.", out)
        self.assertIn("The second is Sakura in HSR Layout. It's rated 4.9, about 1,580 rupees per person, "
                      "and has a quiet zone.", out)
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
        self.assertEqual(payload["lines"][0]["text"], 'He said "</script><b>hi"')
        self.assertEqual(js.count("</script>"), 1, "text must not be able to close the script tag")


if __name__ == "__main__":
    unittest.main()


class SarvamVoice(unittest.TestCase):
    TTS = voice.TTSSettings(api_key="sk-sarvam", base_url="https://sarvam.test")

    def setUp(self):
        voice._TTS_CACHE.clear()

    def _ok(self, audio=b"ID3fake-mp3"):
        import base64
        return FakeResponse(json.dumps({"request_id": "r1", "audios": [base64.b64encode(audio).decode()]}).encode())

    def test_request_and_audio(self):
        seen = {}

        def fake(req, timeout=None):
            seen["url"], seen["headers"], seen["body"] = req.full_url, dict(req.header_items()), json.loads(req.data)
            return self._ok()

        with mock.patch.object(voice.urllib.request, "urlopen", fake):
            audio = voice.synthesize("Hi! I'm Gia.", "en-IN", self.TTS)
        self.assertEqual(audio, b"ID3fake-mp3")
        self.assertEqual(seen["url"], "https://sarvam.test/text-to-speech")
        self.assertEqual(seen["headers"]["Api-subscription-key"], "sk-sarvam")
        b = seen["body"]
        self.assertEqual((b["text"], b["language_code"], b["model"], b["speaker"]), ("Hi! I'm Gia.", "en-IN", "bulbul:v3", "shubh"))
        self.assertEqual((b["pace"], b["temperature"], b["output_audio_codec"]), (1.0, 0.65, "mp3"))
        self.assertNotIn("pitch", b, "pitch is v2-only; v3 rejects it")

    def test_repeated_lines_are_cached(self):
        with mock.patch.object(voice.urllib.request, "urlopen", return_value=self._ok()) as call:
            voice.synthesize("Let me check…", "en-IN", self.TTS)
            voice.synthesize("Let me check…", "en-IN", self.TTS)
        self.assertEqual(call.call_count, 1)

    def test_hindi_and_unknown_languages(self):
        bodies = []

        def fake(req, timeout=None):
            bodies.append(json.loads(req.data)); return self._ok()

        with mock.patch.object(voice.urllib.request, "urlopen", fake):
            voice.synthesize("कल रात 8 बजे", "hi-IN", self.TTS)
            voice.synthesize("Bonjour", "fr-FR", self.TTS)
        self.assertEqual([b["language_code"] for b in bodies], ["hi-IN", "en-IN"])

    def test_errors_fall_back(self):
        with self.assertRaises(VoiceError):
            voice.synthesize("hi", "en-IN", voice.TTSSettings(api_key=""))
        for code, words in ((403, "SARVAM_API_KEY"), (429, "browser voice"), (422, "SARVAM_SPEAKER"), (500, "HTTP 500")):
            with mock.patch.object(voice.urllib.request, "urlopen", side_effect=http_error(code)):
                with self.assertRaises(VoiceError) as ctx:
                    voice.synthesize(f"line {code}", "en-IN", self.TTS)
            self.assertIn(words, str(ctx.exception))
        with mock.patch.object(voice.urllib.request, "urlopen", return_value=FakeResponse(b'{"audios": []}')):
            with self.assertRaises(VoiceError):
                voice.synthesize("empty", "en-IN", self.TTS)

    def test_settings_from_env(self):
        env = {"SARVAM_API_KEY": " k ", "SARVAM_SPEAKER": "Aditya", "SARVAM_PACE": "9", "SARVAM_TEMPERATURE": "oops"}
        with mock.patch.dict("os.environ", env):
            s = voice.load_tts_settings()
        self.assertEqual((s.api_key, s.speaker, s.pace, s.temperature), ("k", "aditya", 2.0, 0.65))

    def test_script_carries_audio(self):
        load = lambda js: json.loads(js.split("var p = ", 1)[1].split(";\n", 1)[0])
        one = load(voice.speak_script(["Hi.", "Bye."], "en-IN", clips=[(b"ID3abc", 0, 2)]))
        self.assertEqual([l["text"] for l in one["lines"]], ["Hi.", "Bye."])
        self.assertEqual(len(one["clips"]), 1, "one natural clip for the whole reply")
        self.assertTrue(one["clips"][0]["audio"].startswith("data:audio/mpeg;base64,"))
        self.assertEqual((one["clips"][0]["from"], one["clips"][0]["to"]), (0, 2))
        per_line = load(voice.speak_script(["Hi.", "Bye."], audio=[b"ID3abc", b"RIFFxyz"]))
        self.assertTrue(per_line["clips"][1]["audio"].startswith("data:audio/wav;base64,"))
        self.assertEqual(load(voice.speak_script(["Hi."]))["clips"], [])
        partial = load(voice.speak_script(["a.", "b."], clips=[(b"ID3", 0, 1)]))
        self.assertEqual(partial["clips"], [], "clips must cover the whole reply; otherwise the browser voice reads it all")



class WordForWord(unittest.TestCase):
    REPLY = ("I'm not sure which GoodFoods outlet is near “Valuable Price Hotel.” Could you tell me the Bengaluru "
             "area (like Indiranagar, Koramangala, etc.) so I can suggest the nearest GoodFoods restaurants?")

    def test_reply_is_read_in_full(self):
        lines = voice.split_for_speech(voice.speakable(self.REPLY))
        self.assertEqual(" ".join(lines), self.REPLY, "every word, in order")
        self.assertEqual(len(lines), 2)

    def test_list_numbers_and_abbreviations_do_not_split(self):
        lines = voice.split_for_speech(voice.speakable(
            "Here are three spots:\n1. GoodFoods Biryani Co. – ₹720/person, 4.7★.\n2. Trattoria – ₹1,210/person.\nWhich one?"))
        self.assertEqual(lines, ["Here are three spots: The first one is Biryani Co. It's rated 4.7, about 720 rupees per person.",
                                 "The second is Trattoria. It's about 1,210 rupees per person.", "Which one?"])

    def test_sentence_after_a_number_still_splits(self):
        lines = voice.split_for_speech("Your code is G F 7 K Q 2 M D for the number ending 3 2 1 0. See you Thursday.")
        self.assertEqual(lines[-1], "See you Thursday.")

    def test_long_sentence_breaks_at_commas(self):
        long = "We have " + ", ".join(f"option number {i}" for i in range(30)) + "."
        lines = voice.split_for_speech(long, max_len=120)
        self.assertTrue(all(len(l) <= 120 for l in lines))
        self.assertEqual(" ".join(lines).replace("  ", " "), long)

    def test_lines_voiced_in_parallel_or_not_at_all(self):
        calls = []

        def fake(text, lang="en-IN", settings=None):
            calls.append(text)
            if text == "bad.":
                raise VoiceError("nope")
            return b"ID3" + text.encode()

        tts = voice.TTSSettings(api_key="k")
        with mock.patch.object(voice, "synthesize", fake):
            self.assertEqual(voice.synthesize_lines(["a.", "b."], "en-IN", tts), [b"ID3a.", b"ID3b."])
            with self.assertRaises(VoiceError):
                voice.synthesize_lines(["a.", "bad."], "en-IN", tts)
        self.assertIsNone(voice.synthesize_lines(["a."], "en-IN", voice.TTSSettings(api_key="")))



class NaturalSpeech(unittest.TestCase):
    def test_days_months_dates_times(self):
        from datetime import date
        out = voice._say_dates_and_times("Sat, 17 Oct 2026 · 20:00. Also 2026-10-22 at 19:30, Thu 22 Oct 8:00 PM, on Sat.",
                                          today=date(2026, 10, 21))
        self.assertEqual(out, "Saturday, 17 October · 8 PM. Also Thursday 22 October at 7:30 PM, "
                              "Thursday 22 October 8 PM, on Saturday.")
        self.assertIn("2027", voice._say_dates_and_times("2027-01-05", today=date(2026, 10, 21)))
        self.assertEqual(voice._say_dates_and_times("He sat down. Oct is busy.", today=date(2026, 10, 21)),
                         "He sat down. Oct is busy.", "only short names next to a date are expanded")

    def test_option_like_the_user_asked(self):
        out = voice.speakable("1. **Skyline Grill**, Indiranagar · ₹1,760 · 4.7★ · bar, bbq platter, pulled pork")
        self.assertEqual(out, "The first one is Skyline Grill in Indiranagar. It's rated 4.7, about 1,760 rupees per person, "
                              "and has a bar, bbq platter and pulled pork.")

    def test_other_numbered_lists_still_read(self):
        self.assertEqual(voice.speakable("Steps:\n1. Pick a time\n2. Confirm"),
                         "Steps: The first one is Pick a time. The second is Confirm.")

    def test_whole_reply_in_one_clip(self):
        self.assertEqual(voice.group_lines(["a" * 50] * 3), [(0, 3)])
        self.assertEqual(voice.group_lines(["a" * 900] * 3, max_chars=2000), [(0, 2), (2, 3)])
        calls = []
        with mock.patch.object(voice, "synthesize", lambda t, l="en-IN", s=None: calls.append(t) or b"ID3"):
            clips = voice.synthesize_groups(["Hi there.", "Which one?"], "en-IN", voice.TTSSettings(api_key="k"))
        self.assertEqual(calls, ["Hi there. Which one?"])
        self.assertEqual(clips, [(b"ID3", 0, 2)])

    def test_echo_of_gias_own_voice(self):
        reply = "Here are three GoodFoods spots in Marathahalli. Which one would you like?"
        self.assertTrue(voice.sounds_like_echo("here are three goodfoods spots in marathahalli which one", reply))
        self.assertFalse(voice.sounds_like_echo("Book the second one for 8 pm tomorrow please", reply))
        self.assertFalse(voice.sounds_like_echo("yes", reply), "short answers are never treated as echoes")
