"""UI walkthrough: drives app.py through every booking step (start -> options -> details -> review ->
booked -> change -> cancel) with a stand-in `streamlit` module and a scripted model. No API key needed.

Run:  python tests/ui_walkthrough.py
"""
import json
import os
import sys
import types
from datetime import datetime
from unittest.mock import MagicMock

os.environ["GOODFOODS_NOW"] = "2026-10-12T16:00"
os.environ["GOODFOODS_DB"] = ":memory:"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))


class State(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError as e:
            raise AttributeError(k) from e

    def __setattr__(self, k, v):
        self[k] = v

    def __delattr__(self, k):
        del self[k]


class Rerun(Exception):
    pass


class FakeSt(types.ModuleType):
    def __init__(self):
        super().__init__("streamlit")
        self.session_state = State()
        self.press = None          # key of the button to "click" this run
        self.typed = None
        self.log = []
        self.column_config = MagicMock()

    def __getattr__(self, name):
        def anything(*a, **k):
            self.log.append((name, a[:1], {kk: k[kk] for kk in ("key",) if kk in k}))
            return MagicMock()
        return anything

    @property
    def sidebar(self):
        return self

    def cache_resource(self, *a, **k):
        return lambda f: f

    def columns(self, spec, **k):
        n = spec if isinstance(spec, int) else len(spec)
        return [self for _ in range(n)]

    def container(self, **k):
        self.log.append(("container", (k.get("key"),), {}))
        return self

    def chat_message(self, *a, **k):
        return self

    def expander(self, *a, **k):
        return self

    def spinner(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def empty(self):
        st_ref = self

        class Slot:   # st.empty(): records what's drawn into it (Gia's stage is drawn this way)
            def markdown(self, body, *a, **k):
                st_ref.log.append(("markdown", (str(body)[:120],), {}))

            def __getattr__(self, name):
                return lambda *a, **k: None
        return Slot()

    def markdown(self, body, *a, **k):
        self.log.append(("markdown", (str(body)[:120],), {}))

    def button(self, label, key=None, on_click=None, args=(), disabled=False, **k):
        self.log.append(("button", (label,), {"key": key, "disabled": disabled}))
        hit = self.press is not None and (key == self.press or label == self.press) and not disabled
        if hit:
            self.press = None
            if on_click:
                on_click(*args)
                raise Rerun()
            return True
        return False

    def download_button(self, label, data, **k):
        self.log.append(("download_button", (label,), {"data": data[:60]}))

    def link_button(self, label, url, **k):
        self.log.append(("link_button", (label,), {"url": url}))

    def pills(self, label, options, format_func=None, default=None, key=None, **k):
        self.log.append(("pills", (label,), {"options": [format_func(o) if format_func else o for o in options][:12],
                                             "default": default}))
        return self.session_state.get(key, default)

    def text_input(self, label, value="", key=None, **k):
        return self.session_state.get(key, value)

    def selectbox(self, label, options, key=None, **k):
        return self.session_state.get(key, options[0])

    def segmented_control(self, label, options, key=None, **k):
        return self.session_state.get(key, options[0])

    def chat_input(self, *a, **k):
        t, self.typed = self.typed, None
        return t

    def toggle(self, label, value=False, key=None, **k):
        self.log.append(("toggle", (label,), {"key": key}))
        return self.session_state.get(key, value) if key else value

    def audio_input(self, label, key=None, **k):
        self.log.append(("audio_input", (label,), {"key": key}))
        return self.session_state.get(key)

    def warning(self, msg, *a, **k):
        self.log.append(("WARNING", (msg,), {}))

    def rerun(self):
        raise Rerun()

    def error(self, msg, *a, **k):
        self.log.append(("ERROR", (msg,), {}))


st = FakeSt()
sys.modules["streamlit"] = st
# streamlit.components.v1.html is how the app speaks: record what it would say.
SPOKEN = []
_components = types.ModuleType("streamlit.components")
_v1 = types.ModuleType("streamlit.components.v1")
_v1.html = lambda body, **k: SPOKEN.append(json.loads(body.split("var p = ", 1)[1].split(";\n", 1)[0]))
_components.v1 = _v1
st.components = _components
sys.modules["streamlit.components"], sys.modules["streamlit.components.v1"] = _components, _v1


class FakeAudio:
    def __init__(self, data):
        self.data = data

    def getvalue(self):
        return self.data

from test_agent import FakeLLM, call, say  # noqa: E402
from goodfoods.agent import ReservationAgent  # noqa: E402
from goodfoods.mcp_client import InProcessTransport, MCPClient  # noqa: E402
from goodfoods.mcp_server import MCPServer  # noqa: E402
from goodfoods.seed import build_database  # noqa: E402
from goodfoods.services import ReservationService  # noqa: E402

SRC = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
service = ReservationService(build_database(":memory:", with_bookings=False))
llm = FakeLLM([])
agent = ReservationAgent(llm, MCPClient(InProcessTransport(MCPServer(service))).initialize(), n_outlets=75)
st.session_state.update(agent=agent, messages=[], usage={"prompt_tokens": 0, "completion_tokens": 0,
                                                         "turns": 0, "llm_ms": 0.0})


def gia_moments():
    """Gia's moments drawn during the last run, in order."""
    import re
    return [re.search(r'data-moment="([a-z]+)"', l[1][0]).group(1) for l in st.log
            if l[0] == "markdown" and 'class="gia-stage"' in l[1][0]]


def gia_is(expected):
    got = gia_moments()
    assert got and got[-1] == expected, f"Gia should be '{expected}', saw {got}"


def run(label, press=None, typed=None, script=None, state=None):
    if script:
        llm.script.extend(script)
    if state:
        st.session_state.update(state)
    st.press, st.typed, st.log = press, typed, []
    g = {"__name__": "__main__"}
    exec_src = SRC.replace("return ReservationService(build_database())", "return SERVICE")
    g["SERVICE"] = service
    for _ in range(4):   # follow reruns (a click, then the model turn, then the redraw)
        try:
            exec(compile(exec_src, "app.py", "exec"), g)
            break
        except Rerun:
            st.press, st.typed = None, None
            continue
    buttons = [l[1][0] for l in st.log if l[0] == "button"]
    keys = [l[1][0] for l in st.log if l[0] == "container" and l[1][0]]
    pills = [(l[1][0], l[2]["options"][:6], l[2]["default"]) for l in st.log if l[0] == "pills"]
    errors = [l[1][0] for l in st.log if l[0] == "ERROR"]
    extra = [(l[0], l[1][0]) for l in st.log if l[0] in ("download_button", "link_button")]
    print(f"\n== {label}")
    print("   containers:", [k for k in keys if k not in ("chat_card", "ctx_col", "ctx_map", "ctx_intro", "ctx_outlet", "ctx_steps")])
    print("   buttons:", buttons[:14])
    if pills:
        for p in pills:
            print("   pills:", p)
    if extra:
        print("   other:", extra)
    if errors:
        print("   ERRORS:", errors)
    print("   gia:", gia_moments()[-1:] )
    print("   last msgs:", [(m["role"], m["content"][:70]) for m in st.session_state.messages[-2:]])
    print("   pending:", agent.pending.tool if agent.pending else None)


# 1 start
run("start")
gia_is("greeting")
# 2 occasion -> model recommends
run("occasion -> options", press="**Date night** · romantic, rooftop or quiet",
    script=[call("recommend_restaurants", {"occasion": "date", "party_size": 2, "date": "2026-10-17", "time": "20:00"}),
            say("Three lovely options free at 8 PM Saturday.")])
assert "searching" in gia_moments(), "Gia should search while the model works"
gia_is("options")
# 3 choose first card
first_key = next(k for k in ("pick_1_0",))
run("choose card -> details form", press=first_key)
gia_is("picked")
# 4 fill name/phone and review (needs a time picked: set via pills state)
rid = st.session_state.picked
st.session_state[f"bk_{rid}_day"] = "2026-10-17"
st.session_state[f"bk_{rid}_party"] = "2"
st.session_state[f"bk_{rid}_time_2026-10-17_2"] = "20:00"
st.session_state["f_name"] = "Asha Rao"
st.session_state["f_phone"] = "98765 43210"
args = {"restaurant_id": rid, "date": "2026-10-17", "time": "20:00", "party_size": 2,
        "customer_name": "Asha Rao", "phone": "9876543210"}
run("review booking -> preview", press="f_review",
    script=[call("make_reservation", args), say("Here's your booking — shall I confirm?")])
gia_is("confirm")
# 5 confirm
run("confirm -> ticket", press="rv_yes", script=[call("make_reservation", args), say("You're booked!")])
gia_is("booked")
code = next(iter(agent.known_codes), None)
print("   booked code:", code, "rows:", service.db.query("SELECT COUNT(*) n FROM reservations")[0]["n"])
# 6 change from ticket -> lookup -> list
# phone-only lookup: codes are masked, so the card must offer "Unlock" rather than change/cancel
run("change this booking -> phone-only lookup (masked)", press="Change this booking",
    script=[call("lookup_reservation", {"phone": "9876543210"}), say("I found your booking. What's your code?")])
assert not any(k.get("key") == "mod_0" for _, _, k in st.log), "change button shown for a masked code"
assert any(k.get("key") == "unlock_0" for _, _, k in st.log), "unlock control missing for a masked code"
print("   masked lookup: change/cancel hidden, unlock shown")
run("unlock with code -> full lookup", press="unlock_0", state={"unlock_code_0": code},
    script=[call("lookup_reservation", {"phone": "9876543210", "confirmation_code": code}),
            say("I found your booking.")])
run("pick change -> modify form", press="mod_0")
st.session_state[f"mod_{code}_time_2026-10-17_2"] = "21:00"
run("review change -> preview", press="mod_review",
    script=[call("modify_reservation", {"confirmation_code": code, "phone": "9876543210", "new_date": "2026-10-17",
                                        "new_time": "21:00", "new_party_size": 2}), say("Move it to 9 PM?")])
run("confirm change -> ticket", press="rv_yes",
    script=[call("modify_reservation", {"confirmation_code": code, "phone": "9876543210", "new_date": "2026-10-17",
                                        "new_time": "21:00", "new_party_size": 2}), say("Updated!")])
print("   db time now:", service.db.query("SELECT res_time FROM reservations")[0]["res_time"])
# 7 cancel from ticket -> preview -> keep, then cancel -> confirm
run("cancel it -> preview", press="Cancel it",
    script=[call("cancel_reservation", {"confirmation_code": code, "phone": "9876543210"}), say("Cancel it?")])
run("keep my booking", press="rv_back")
run("manage form shown again", press=None)
# 8 edit details path: new chat, explore jump
run("new chat", press="New chat")
run("bad phone validation", state={"picked": "BLR-001", "f_phone": "123", f"bk_BLR-001_time_2026-10-12_2": "19:00"},
    press="f_review")


# 9 voice journey: greeting -> say what you want -> options read aloud -> "help me choose" -> pick -> next step
from goodfoods import voice  # noqa: E402

for k in ("picked", "focus_override", "f_phone"):   # leave the bad-phone form from step 8
    st.session_state.pop(k, None)
run("voice: start screen", press=None)
assert any(l[2].get("key") == "start_voice" for l in st.log if l[0] == "button"), "no 'Talk to Gia' button"
SPOKEN.clear()
run("voice: tap 'Talk to Gia'", press="start_voice")
assert SPOKEN and "What can I do for you today?" in SPOKEN[-1]["text"], SPOKEN
assert any(l[0] == "audio_input" for l in st.log), "mic not shown with voice on"
print("   spoken:", SPOKEN[-1]["text"][:90])

said = "Tomorrow is my anniversary, I need a table for 2 to surprise my wife"
voice.transcribe = lambda data, settings=None, **k: said
run("voice: guest speaks", state={"mic_0": FakeAudio(b"RIFF....fake wav")},
    script=[call("recommend_restaurants", {"occasion": "anniversary", "party_size": 2, "date": "tomorrow"}),
            say("Happy anniversary! Three lovely options for tomorrow:\n1. Sakura, HSR Layout · ₹1,580 · 4.9★\n"
                "Which one would you like?")])
assert agent.history[-4]["content"] == said or any(m.get("content") == said for m in agent.history), "transcript not sent"
assert st.session_state.messages[-2]["content"] == "🎤 " + said
assert "1,580 rupees" in SPOKEN[-1]["text"] and "4.9 stars" in SPOKEN[-1]["text"], SPOKEN[-1]
print("   spoken:", SPOKEN[-1]["text"][:90])

run("voice: same recording is not sent twice", state={"mic_0": FakeAudio(b"RIFF....fake wav")})
assert st.session_state.messages[-2]["content"] == "🎤 " + said

run("voice: help me choose", press="Help me choose",
    script=[say("For a quiet anniversary surprise I'd pick Sakura in HSR Layout: it's top rated and has a quiet zone.")])
gia_is("suggestions")
assert "Sakura" in SPOKEN[-1]["text"]
print("   spoken:", SPOKEN[-1]["text"][:90])

pick_key = next(l[2]["key"] for l in reversed(st.log) if l[0] == "button" and str(l[2].get("key", "")).startswith("pick_"))
run("voice: choose a card", press=pick_key)
assert "Choose your day, guests and time" in SPOKEN[-1]["text"], SPOKEN[-1]
print("   spoken:", SPOKEN[-1]["text"][:90])


def mic_fails(*a, **k):
    raise voice.VoiceError("I didn't catch that. Tap the mic and speak a little closer.")


voice.transcribe = mic_fails
assert "listening" not in gia_moments()
run("voice: mic error is shown and spoken", state={f"mic_{st.session_state.mic_n}": FakeAudio(b"noise")})
assert any(l[0] == "WARNING" for l in st.log) and "didn't catch" in SPOKEN[-1]["text"]
gia_is("thinking")

run("voice: turn off hides the mic", state={"voice_on": False}, press=None)
assert not any(l[0] == "audio_input" for l in st.log), "mic shown with voice off"
print("\nvoice journey OK")
