"""Gia, the on-screen host: which moment she is in, and the animated stage that shows it.

No Streamlit here, so the logic is unit-tested. `choose_moment` reads the same state the chat
controls use (current step, the last agent trace, any pending confirmation) and picks one of
the moments below. `stage_html` returns the stage: a pose on a lit spotlight, a speech bubble
and the moment's motion, all in CSS so it runs inside Streamlit's markdown without JavaScript.
The talking bounce is switched on by the speech script in `voice.py` while a reply is read aloud.

The pose images live in static/gia/. To use new art, replace those files and keep the names.
"""
from __future__ import annotations

import html
import random
from dataclasses import dataclass

# Native heights of the pose cut-outs (pixels in the source sheet). Every pose is drawn at the
# same scale, so Gia's head stays the same size when the pose changes.
POSE_HEIGHT = {"greeting": 202, "searching": 202, "options": 214, "confirm": 210, "thinking": 237,
               "suggestions": 246, "travel": 254, "happy": 254}
POSE_ALT = {"greeting": "Gia waving hello", "searching": "Gia searching with a magnifying glass",
            "options": "Gia holding a map", "confirm": "Gia giving a thumbs up", "thinking": "Gia thinking",
            "suggestions": "Gia with a lightbulb idea", "travel": "Gia relaxed in sunglasses",
            "happy": "Gia jumping for joy"}


@dataclass(frozen=True)
class Moment:
    pose: str
    line: str
    loop: str = "float"         # idle style: float | sway | tilt | lean | none
    once: str | None = None     # one-off reaction: wave | hop | nod | bump | jump
    fx: str | None = None       # rings | bulb | sparkle | confetti
    idle: bool = True           # small bounce every few seconds
    then: str | None = None     # pose to settle into after the reaction (booked -> relaxed)


MOMENTS: dict[str, Moment] = {
    "greeting": Moment("greeting", "Hi! I'm Gia. What can I do for you today?", once="wave"),
    "listening": Moment("thinking", "I'm listening…", fx="rings", idle=False),
    "searching": Moment("searching", "Finding the perfect spot…", loop="sway", idle=False),
    "options": Moment("options", "Here are some great picks!", once="hop"),
    "suggestions": Moment("suggestions", "Here's my pick for you!", once="nod", fx="bulb"),
    "picked": Moment("greeting", "Great choice! Pick a time below and add your details.", once="hop"),
    "thinking": Moment("thinking", "One quick question…", loop="tilt", idle=False),
    "full": Moment("thinking", "That time is full, but I have other options.", loop="tilt", idle=False),
    "confirm": Moment("confirm", "Check your details, then confirm.", once="bump", fx="sparkle"),
    "booked": Moment("happy", "You're booked! Enjoy your meal.", loop="none", once="jump", fx="confetti",
                     idle=False, then="travel"),
    "updated": Moment("happy", "All changed! See you then.", once="hop"),
    "cancelled": Moment("greeting", "Cancelled. Hope to see you soon!", once="wave"),
    "manage": Moment("searching", "Let's find your booking.", loop="sway", idle=False),
    "found": Moment("confirm", "Found it! What would you like to do?", once="bump"),
    "modify": Moment("greeting", "Pick the new day or time below.", once="hop"),
    "sorry": Moment("thinking", "Sorry, I couldn't finish that. Please try again.", loop="tilt", idle=False),
    "idle": Moment("greeting", "Anything else I can help with?"),
}


def choose_moment(step: str, trace: list | None = None, pending_tool: str | None = None,
                  done_status: str | None = None, reply: str = "") -> str:
    """Pick Gia's moment from the chat state.

    step         the controls being shown (start, options, details, review, done, manage, modify, chat)
    trace        events of Gia's latest turn (objects with .kind, .name, .detail)
    pending_tool the write waiting for the guest's yes, if any
    done_status  confirmed | modified | cancelled when a write just finished
    reply        Gia's latest reply text
    """
    trace = trace or []
    tool_events = [e for e in trace if e.kind == "tool"]
    if step == "start":
        return "greeting"
    if pending_tool:
        return "confirm"
    if step == "done":
        return {"confirmed": "booked", "modified": "updated", "cancelled": "cancelled"}.get(done_status or "", "booked")
    if step == "details":
        return "picked"
    if step == "modify":
        return "modify"
    if any(e.kind == "llm" and e.name == "error" for e in trace):
        return "sorry"
    if any(e.detail.get("error") and (e.detail.get("result") or {}).get("error") == "NOT_AVAILABLE"
           for e in tool_events):
        return "full"
    if any((e.detail.get("result") or {}).get("recommendations") or (e.detail.get("result") or {}).get("restaurants")
           for e in tool_events if not e.detail.get("error")):
        return "options"
    if step == "manage":
        return "found" if any(e.name == "lookup_reservation" and not e.detail.get("error") for e in tool_events) else "manage"
    if step == "options":
        return "suggestions"   # cards still live and Gia answered without a new search: she's advising
    if reply.strip().endswith("?"):
        return "thinking"
    return "idle"


# ----------------------------------------------------------------------------- rendering

# Sizes and confetti paths live in the stylesheet as plain classes, not in style="" attributes:
# Streamlit's markdown keeps ordinary inline styles but may drop CSS variables written there.
CONFETTI_PIECES = 26


def _img(base: str, pose: str, cls: str = "") -> str:
    return (f'<img class="gia-pose p-{pose} {cls}" src="{html.escape(base)}/{pose}.png" '
            f'alt="{html.escape(POSE_ALT[pose])}" draggable="false">')


def _confetti() -> str:
    return '<div class="gia-confetti" aria-hidden="true">' + "<i></i>" * CONFETTI_PIECES + "</div>"


def _generated_css() -> str:
    rules = [f".gia-pose.p-{pose} {{ height: calc({h}px * var(--gia-k)); }}" for pose, h in POSE_HEIGHT.items()]
    rnd = random.Random(7)   # fixed, so every redraw gets the same burst
    colors = ["#F05A7E", "#F3C14B", "#4F7BE8", "#9B6DE0", "#2FA67A", "#F08A3C"]
    for i in range(CONFETTI_PIECES):
        rules.append(f".gia-confetti i:nth-child({i + 1}) {{ --dx: {rnd.randint(-110, 120)}px; --dy: {rnd.randint(-150, -50)}px; "
                     f"--r: {rnd.randint(-300, 300)}deg; background: {colors[i % len(colors)]}; "
                     f"animation-delay: {rnd.randint(0, 15) / 100:.2f}s; }}")
    return "\n".join(rules)


def _sparkles() -> str:
    spots = [(132, 30, 16), (150, 80, 11), (26, 56, 12), (152, 134, 9), (18, 126, 10), (112, 10, 9)]
    return ('<div class="gia-sparks" aria-hidden="true">' + "".join(
        f'<i style="left:{x}px;top:{y}px;width:{s}px;height:{s}px;animation-delay:{i * 0.09:.2f}s"></i>'
        for i, (x, y, s) in enumerate(spots)) + "</div>")


def stage_html(moment: str, base: str = "app/static/gia", line: str | None = None) -> str:
    """The stage for one moment. `base` is where the pose images are served from."""
    m = MOMENTS.get(moment, MOMENTS["idle"])
    text = html.escape(line or m.line)
    poses = _img(base, m.pose, "gia-first" if m.then else "")
    if m.then:
        poses += _img(base, m.then, "gia-then")
    fx = ""
    if m.fx == "rings":
        fx = '<div class="gia-rings" aria-hidden="true"><i></i><i></i></div>'
    elif m.fx == "bulb":
        fx = '<div class="gia-bulb" aria-hidden="true"></div>'
    elif m.fx == "sparkle":
        fx = _sparkles()
    elif m.fx == "confetti":
        fx = _confetti()
    once = f" gia-{m.once}" if m.once else ""
    idle = " gia-idle" if m.idle else ""
    return (f'<div class="gia-stage" data-moment="{moment}" data-loop="{m.loop}">'
            f'<div class="gia-spot">{fx if m.fx == "rings" else ""}<div class="gia-disc"></div><div class="gia-floor"></div>'
            f'<div class="gia-loop"><div class="gia-idlewrap{idle}"><div class="gia-once{once}"><div class="gia-talk">'
            f'<div class="gia-breathe">{poses}</div></div></div></div></div>'
            f'{"" if m.fx == "rings" else fx}</div>'
            f'<div class="gia-bubble" aria-hidden="true"><span>{text}</span>'
            f'<span class="gia-bars"><i></i><i></i><i></i></span></div></div>')


STAGE_CSS = """
.gia-stage { --gia-k: .8; position: relative; height: 240px; margin: 4px 0 8px; border-radius: 18px; overflow: hidden;
  display: grid; grid-template-columns: 178px minmax(0, 1fr); align-items: center; color: #17201C;
  background: radial-gradient(118px 118px at 89px 62%, #FFFFFF 0 52%, rgba(255,255,255,.45) 78%, transparent 100%),
              linear-gradient(180deg, #F1F5FA, #E6EDF5); }
.gia-spot { position: relative; height: 100%; }
.gia-disc { position: absolute; left: 14px; bottom: 14px; width: 150px; height: 150px; border-radius: 50%;
  background: radial-gradient(circle at 50% 42%, #FFFFFF 0 58%, #EEF3F8 100%); box-shadow: inset 0 -6px 14px rgba(23,32,28,.05); }
.gia-floor { position: absolute; left: 48px; bottom: 13px; width: 82px; height: 10px; border-radius: 50%;
  background: rgba(23,32,28,.16); filter: blur(3px); }
.gia-loop, .gia-idlewrap, .gia-once, .gia-talk, .gia-breathe { position: absolute; inset: 0; transform-origin: 50% 92%; }
.gia-pose { position: absolute; left: 50%; bottom: 12px; height: 160px; width: auto;
  transform: translateX(-50%); user-select: none; pointer-events: none; animation: giaPoseIn .46s cubic-bezier(.2,.9,.25,1.25) both; }
.gia-pose.gia-first { animation: giaPoseIn .46s cubic-bezier(.2,.9,.25,1.25) both, giaFadeOut .4s ease-in 2.5s forwards; }
.gia-pose.gia-then { opacity: 0; animation: giaPoseIn .5s cubic-bezier(.2,.9,.25,1.25) 2.75s forwards; }
.gia-breathe { animation: giaBreathe 3.6s ease-in-out infinite; }
.gia-stage[data-loop="float"] .gia-loop { animation: giaFloat 3.6s ease-in-out infinite; }
.gia-stage[data-loop="sway"] .gia-loop { animation: giaSway 1.6s ease-in-out infinite; }
.gia-stage[data-loop="tilt"] .gia-loop { animation: giaTilt 2.8s ease-in-out infinite; }
.gia-stage[data-loop="lean"] .gia-loop { animation: giaLean 4.4s ease-in-out infinite; }
.gia-stage[data-loop="float"] .gia-floor { animation: giaFloor 3.6s ease-in-out infinite; }
.gia-idle { animation: giaTwitch 7s ease-in-out 3s infinite; }
.gia-once.gia-wave { animation: giaWave 1.2s cubic-bezier(.3,.7,.3,1) both; }
.gia-once.gia-hop { animation: giaHop .75s cubic-bezier(.3,.7,.4,1) .15s both; }
.gia-once.gia-nod { animation: giaNod .95s ease-in-out .15s both; }
.gia-once.gia-bump { animation: giaBump .65s cubic-bezier(.2,.9,.3,1.4) .15s both; }
.gia-once.gia-jump { animation: giaJump 1.1s cubic-bezier(.3,.6,.4,1) .1s 2 both; }
.gia-stage.gia-talking .gia-talk { animation: giaTalk .3s ease-in-out infinite alternate; }
.gia-rings { position: absolute; left: 89px; bottom: 89px; }
.gia-rings i { position: absolute; left: -75px; top: -75px; width: 150px; height: 150px; border-radius: 50%;
  border: 2px solid #1E5A47; animation: giaRing 1.8s cubic-bezier(.2,.6,.3,1) infinite; }
.gia-rings i:nth-child(2) { animation-delay: .9s; }
.gia-bulb { position: absolute; left: 24px; top: 22px; width: 50px; height: 50px; border-radius: 50%; opacity: 0; pointer-events: none;
  background: radial-gradient(circle, rgba(255,214,90,.95) 0, rgba(255,214,90,.35) 45%, transparent 70%); animation: giaBulb 1.1s ease-in-out .2s 3; }
.gia-sparks { position: absolute; inset: 0; pointer-events: none; }
.gia-sparks i { position: absolute; background: #F2B930; opacity: 0; animation: giaSpark 1s ease-out .2s forwards;
  clip-path: polygon(50% 0, 61% 39%, 100% 50%, 61% 61%, 50% 100%, 39% 61%, 0 50%, 39% 39%); }
.gia-confetti { position: absolute; inset: 0; pointer-events: none; }
.gia-confetti i { position: absolute; left: 86px; top: 96px; width: 7px; height: 11px; border-radius: 2px; opacity: 0;
  animation: giaConfetti 1.6s cubic-bezier(.2,.7,.4,1) forwards; }
.gia-bubble { position: relative; justify-self: start; max-width: 360px; margin-right: 14px; padding: 12px 14px; background: #FFFFFF;
  border-radius: 16px; font-weight: 600; font-size: 15px; line-height: 1.38;
  box-shadow: 0 1px 2px rgba(23,32,28,.08), 0 10px 22px rgba(23,32,28,.08); animation: giaBubble .4s cubic-bezier(.2,.9,.3,1.2) .1s both; }
.gia-bubble::before { content: ""; position: absolute; left: -6px; top: 50%; width: 14px; height: 14px; background: #FFFFFF;
  transform: translateY(-50%) rotate(45deg); border-radius: 2px 0 2px 2px; }
.gia-bars { display: inline-flex; gap: 2px; align-items: flex-end; height: 12px; margin-left: 6px; vertical-align: middle; opacity: 0; transition: opacity .2s; }
.gia-bars i { width: 3px; height: 4px; border-radius: 2px; background: #1E5A47; }
.gia-stage.gia-talking .gia-bars { opacity: 1; }
.gia-stage.gia-talking .gia-bars i { animation: giaBar .45s ease-in-out infinite alternate; }
.gia-stage.gia-talking .gia-bars i:nth-child(2) { animation-delay: .15s; }
.gia-stage.gia-talking .gia-bars i:nth-child(3) { animation-delay: .3s; }
@keyframes giaPoseIn { 0% { transform: translateX(-50%) translateY(6px) scale(.9,.86); opacity: 0; }
  55% { transform: translateX(-50%) translateY(-3px) scale(1.03,1.05); opacity: 1; } 100% { transform: translateX(-50%); opacity: 1; } }
@keyframes giaFadeOut { to { opacity: 0; transform: translateX(-50%) scale(.94); } }
@keyframes giaBreathe { 0%, 100% { transform: scale(1,1); } 50% { transform: scale(1.006,1.014); } }
@keyframes giaFloat { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-6px); } }
@keyframes giaFloor { 0%, 100% { transform: scaleX(1); opacity: 1; } 50% { transform: scaleX(.88); opacity: .7; } }
@keyframes giaSway { 0%, 100% { transform: translateX(-6px) rotate(-2deg); } 50% { transform: translateX(6px) rotate(2deg); } }
@keyframes giaTilt { 0%, 100% { transform: rotate(-3deg); } 50% { transform: rotate(3deg) translateY(-2px); } }
@keyframes giaLean { 0%, 100% { transform: rotate(0) translateY(0); } 50% { transform: rotate(1.5deg) translateY(-4px); } }
@keyframes giaTwitch { 0%, 88%, 100% { transform: none; } 91% { transform: scale(1.03,.96); } 95% { transform: translateY(-5px) scale(.99,1.02); } }
@keyframes giaWave { 0% { transform: translateX(46px) rotate(7deg); opacity: 0; } 32% { transform: translateX(0) rotate(-4deg); opacity: 1; }
  52% { transform: rotate(3deg); } 72% { transform: rotate(-1.5deg); } 100% { transform: rotate(0); } }
@keyframes giaHop { 0% { transform: none; } 18% { transform: scale(1.06,.92); } 48% { transform: translateY(-16px) scale(.97,1.04); }
  78% { transform: scale(1.04,.95); } 100% { transform: none; } }
@keyframes giaNod { 0%, 100% { transform: rotate(0); } 25% { transform: rotate(-5deg); } 55% { transform: rotate(4deg); } 80% { transform: rotate(-1deg); } }
@keyframes giaBump { 0% { transform: scale(1); } 40% { transform: scale(1.08); } 100% { transform: scale(1); } }
@keyframes giaJump { 0% { transform: none; } 15% { transform: scale(1.08,.9); } 45% { transform: translateY(-30px) scale(.95,1.06); }
  75% { transform: scale(1.07,.92); } 100% { transform: none; } }
@keyframes giaTalk { from { transform: translateY(0) rotate(-.4deg); } to { transform: translateY(-2px) rotate(.4deg) scale(1.01); } }
@keyframes giaRing { from { transform: scale(.62); opacity: .6; } to { transform: scale(1.32); opacity: 0; } }
@keyframes giaBulb { 0%, 100% { opacity: 0; transform: scale(.7); } 50% { opacity: 1; transform: scale(1.2); } }
@keyframes giaSpark { 0% { transform: scale(0); opacity: 0; } 35% { transform: scale(1.15) rotate(20deg); opacity: 1; } 100% { transform: scale(0) rotate(45deg); opacity: 0; } }
@keyframes giaConfetti { 0% { transform: translate(0,0) rotate(0); opacity: 1; }
  55% { transform: translate(var(--dx), var(--dy)) rotate(var(--r)); opacity: 1; }
  100% { transform: translate(calc(var(--dx) * 1.25), calc(var(--dy) + 160px)) rotate(calc(var(--r) * 2)); opacity: 0; } }
@keyframes giaBubble { from { transform: translateY(6px) scale(.97); opacity: 0; } to { transform: none; opacity: 1; } }
@keyframes giaBar { from { height: 3px; } to { height: 12px; } }
@media (max-width: 640px) {
  .gia-stage { --gia-k: .7; height: 214px; grid-template-columns: 150px minmax(0, 1fr); }
  .gia-disc { width: 128px; height: 128px; left: 11px; } .gia-floor { left: 37px; width: 72px; }
  .gia-rings { left: 75px; bottom: 77px; } .gia-confetti i { left: 72px; }
  .gia-bubble { font-size: 14px; margin-right: 10px; }
}
@media (prefers-reduced-motion: reduce) {
  .gia-stage *, .gia-stage { animation: none !important; transition: none !important; }
  .gia-pose { opacity: 1; } .gia-pose.gia-first { display: none; } .gia-pose.gia-then { opacity: 1; }
  .gia-confetti, .gia-sparks, .gia-bulb { display: none; }
}
"""
STAGE_CSS += _generated_css() + "\n"
