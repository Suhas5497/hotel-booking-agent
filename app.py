"""GoodFoods — Streamlit front end ("Concierge Split" layout).

Run:  python -m streamlit run app.py

Left: the conversation with Gia, with controls that change at each step of a booking
(start -> options -> date/guests/time/contact -> review -> booked, plus change or cancel).
Right: the outlet in focus, a map, and a plain-language list of what Gia just did.

User-friendly agent principles applied here:
  * show the right controls for the step - tap, don't type
  * only offer what is really possible (free time slots only)
  * preview before anything is written; the guest's confirm is the only way to book
  * always a way back (Edit / Back / Keep my booking), and typing always works
  * every choice is sent to Gia as a message, so the model still decides the tool
"""
from __future__ import annotations

import html
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from goodfoods import catalog, flow
from goodfoods.agent import ReservationAgent
from goodfoods.config import load_llm_settings, now
from goodfoods.llm import LLMClient
from goodfoods.mcp_client import InProcessTransport, MCPClient
from goodfoods.mcp_server import MCPServer
from goodfoods.seed import build_database
from goodfoods.services import CODE_RE, ReservationService, ToolError, normalize_phone, validate_name
from goodfoods.timeparse import ParseError, human_time, parse_date, parse_time, snap_to_slot
from goodfoods.ui_art import cover_svg

ANYWHERE = "Anywhere in Bengaluru"

PAGES = ["Reserve with Gia", "Explore outlets", "Bookings & metrics"]
AVATARS = {"assistant": ":material/room_service:", "user": ":material/person:"}

st.set_page_config(page_title="GoodFoods · Reserve with Gia", page_icon=":material/restaurant:",
                   layout="wide", initial_sidebar_state="collapsed")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,500;6..72,600&family=Manrope:wght@400;500;600;700&display=swap');
:root { --gf-ink:#17201C; --gf-muted:#5B6660; --gf-line:#E3E6E1; --gf-accent:#1E5A47; --gf-tint:#E6F0EB; }
.stApp { background:#F4F5F2; }
.stApp, .stMarkdown, .stMarkdown p, button, input, textarea { font-family:'Manrope', system-ui, sans-serif !important; }
.block-container { padding-top:1.4rem; max-width:1320px; }
header[data-testid="stHeader"] { background:transparent; }

/* header */
.gf-wordmark { font-family:'Newsreader', Georgia, serif; font-size:28px; font-weight:600; letter-spacing:-0.01em; color:var(--gf-ink); }
.gf-status { display:flex; justify-content:flex-end; align-items:center; gap:8px; font-size:13px; color:#4A5650; }
.gf-dot { width:8px; height:8px; border-radius:50%; display:inline-block; }

/* cards */
.st-key-chat_card, .st-key-ctx_outlet, .st-key-ctx_preview, .st-key-ctx_steps, .st-key-ctx_intro {
  background:#FFFFFF; border-radius:20px; padding:24px 26px;
  box-shadow:0 1px 2px rgba(23,32,28,.04), 0 8px 24px rgba(23,32,28,.06); }
.gf-h1 { font-family:'Newsreader', Georgia, serif; font-size:30px; font-weight:500; letter-spacing:-0.01em; color:var(--gf-ink); margin:0; line-height:1.2; }
.gf-sub { font-size:14px; color:var(--gf-muted); margin-top:4px; }
.gf-label { font-size:12px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; color:var(--gf-muted); margin-bottom:10px; }
.gf-title { font-family:'Newsreader', Georgia, serif; font-size:24px; font-weight:600; color:var(--gf-ink); line-height:1.2; }
.gf-meta { font-size:14px; color:var(--gf-muted); margin-top:4px; }
.gf-pills { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }
.gf-pill { font-size:12px; padding:4px 10px; border-radius:999px; background:var(--gf-tint); color:#143D30; font-weight:600; }
.gf-slotday { font-size:13px; font-weight:600; color:var(--gf-ink); margin:16px 0 6px; }
.gf-kv { display:grid; grid-template-columns:auto 1fr; row-gap:10px; column-gap:18px; font-size:14px; margin-bottom:14px; }
.gf-kv .k { color:var(--gf-muted); } .gf-kv .v { font-weight:600; color:var(--gf-ink); }
.gf-note { display:flex; gap:8px; align-items:center; font-size:12px; color:#4A5650; margin-top:4px; }
.gf-booked { background:var(--gf-tint); border-radius:14px; padding:14px 16px; color:#143D30; font-size:14px; line-height:1.5; }
.gf-booked b { font-size:18px; letter-spacing:.04em; }
.gf-step { display:flex; gap:10px; align-items:baseline; font-size:13px; margin:8px 0; }
.gf-step code { font-size:12px; background:#F1F3EF; padding:2px 6px; border-radius:6px; color:var(--gf-ink); }
.gf-step .n { color:var(--gf-muted); }
.gf-stat { display:flex; justify-content:space-between; font-size:14px; padding:6px 0; border-bottom:1px solid #EEF0EC; }
.gf-stat:last-child { border-bottom:none; } .gf-stat b { font-weight:700; }

/* recommendation cards inside chat */
.gf-recs { display:grid; grid-template-columns:repeat(auto-fit, minmax(170px, 1fr)); gap:12px; margin:6px 0 4px; }
.gf-rec { border:1px solid var(--gf-line); border-radius:14px; padding:14px; display:flex; flex-direction:column; gap:4px; background:#FFFFFF; }
.gf-rec.top { border:2px solid var(--gf-accent); }
.gf-rec b { font-size:14px; color:var(--gf-ink); }
.gf-rec .a { font-size:12px; color:var(--gf-muted); }
.gf-rec .p { font-size:13px; font-weight:600; color:var(--gf-ink); }
.gf-rec .w { font-size:12px; color:#143D30; }
.gf-rec .t, .gf-recbody .t { font-size:11px; color:#4A5650; margin-top:2px; }
[class*="st-key-rec_"], [class*="st-key-recsel_"] { background:#FFFFFF; border:1px solid var(--gf-line); border-radius:14px; padding:14px; gap:10px; }
[class*="st-key-recsel_"] { border:2px solid var(--gf-accent); background:#F7FBF9; }
.gf-recbody { display:flex; flex-direction:column; gap:4px; }
.gf-recbody b { font-size:14px; color:var(--gf-ink); }
.gf-recbody .a { font-size:12px; color:var(--gf-muted); }
.gf-recbody .p { font-size:13px; font-weight:600; color:var(--gf-ink); }
.gf-recbody .w { font-size:12px; color:#143D30; }
[class*="st-key-rec_"] .stButton button, [class*="st-key-recsel_"] .stButton button { min-height:36px; border-radius:10px; font-weight:600; }

/* chat bubbles */
[data-testid="stChatMessage"] { background:transparent; padding:6px 0; }
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) { flex-direction:row-reverse; }
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) [data-testid="stChatMessageContent"] {
  background:#17201C; border-radius:18px 18px 4px 18px; padding:12px 16px; max-width:75%; margin-left:auto; flex-grow:0; }
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) [data-testid="stChatMessageContent"] p { color:#FFFFFF; }
[data-testid="stChatMessageAvatarUser"] { display:none; }
[data-testid="stChatMessageAvatarAssistant"] { background:var(--gf-tint); color:#143D30; }
[data-testid="stChatMessageContent"] p, [data-testid="stChatMessageContent"] li { font-size:15px; line-height:1.55; }

/* buttons */
.stButton button { border-radius:999px; border:1px solid #D5DAD3; background:#FFFFFF; color:var(--gf-ink); font-weight:500; min-height:40px; }
.stButton button:hover { border-color:var(--gf-accent); color:#143D30; }
.stButton button[data-testid="stBaseButton-primary"] { background:var(--gf-accent); border-color:var(--gf-accent); color:#FFFFFF; border-radius:12px; font-weight:700; min-height:48px; }
.st-key-slots .stButton button { border-radius:8px; min-height:38px; padding:0 4px; background:#F1F3EF; border-color:#F1F3EF; color:#4A5650; }
.st-key-slots .stButton button[data-testid="stBaseButton-primary"] { background:var(--gf-accent); border-color:var(--gf-accent); color:#FFFFFF; min-height:38px; border-radius:8px; }
.st-key-slots .stButton button:disabled { background:#FFFFFF; border:1px dashed #C9CFC7; color:#6F7973; text-decoration:line-through; }
.st-key-ctx_map { background:#FFFFFF; border-radius:20px; padding:20px 22px;
  box-shadow:0 1px 2px rgba(23,32,28,.04), 0 8px 24px rgba(23,32,28,.06); }
.gf-legend { display:flex; flex-wrap:wrap; gap:14px; font-size:12px; color:#4A5650; margin-top:8px; }
.gf-legend i { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; vertical-align:-1px; }
.gf-focus { display:flex; gap:12px; align-items:center; background:#F4F7F5; border:1px solid var(--gf-line);
  border-radius:14px; padding:8px 12px; margin:8px 0 12px; font-size:14px; color:var(--gf-ink); }
.gf-focus-art { width:84px; flex:none; }
.gf-focus span { font-size:12px; color:var(--gf-muted); }
/* step controls */
.st-key-flow_box, .st-key-review_card { border:1px solid var(--gf-line); border-radius:16px; padding:18px; background:#FCFCFB; }
[class*="st-key-bk_card_"] { border:1px solid var(--gf-line); border-radius:14px; padding:14px 16px; }
.st-key-ticket { background:var(--gf-tint); border-radius:18px; padding:20px 22px; }
.gf-ticket-head { display:flex; gap:12px; align-items:center; }
.gf-check { width:40px; height:40px; border-radius:50%; background:var(--gf-accent); color:#FFFFFF; display:flex;
  align-items:center; justify-content:center; font-weight:700; font-size:20px; flex:none; }
.gf-ticket-grid { display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:10px; background:#FFFFFF;
  border-radius:14px; padding:14px; margin-top:14px; text-align:center; }
.gf-ticket-grid span { display:block; font-size:12px; color:var(--gf-muted); }
.gf-ticket-grid b { font-size:15px; color:var(--gf-ink); }
[data-testid="stPills"] button { border-radius:10px; }
/* keep the context panel in view while the chat scrolls */
.st-key-ctx_col { position:sticky; top:1rem; }
[data-testid="stColumn"]:has(.st-key-ctx_col) { align-self:stretch; }
[data-testid="stMetric"] { background:#FFFFFF; border-radius:16px; padding:18px 20px; box-shadow:0 1px 2px rgba(23,32,28,.04), 0 6px 18px rgba(23,32,28,.05); }
</style>
""", unsafe_allow_html=True)


# ----------------------------------------------------------------------------- wiring
@st.cache_resource(show_spinner="Setting up 75 GoodFoods outlets…")
def get_service() -> ReservationService:
    return ReservationService(build_database())


def get_agent() -> ReservationAgent:
    if "agent" not in st.session_state:
        service = get_service()
        mcp = MCPClient(InProcessTransport(MCPServer(service))).initialize()
        st.session_state.agent = ReservationAgent(LLMClient(load_llm_settings()), mcp,
                                                  n_outlets=service.db.restaurant_count())
        st.session_state.messages = []
        st.session_state.usage = {"prompt_tokens": 0, "completion_tokens": 0, "turns": 0, "llm_ms": 0.0}
    return st.session_state.agent


def queue(text: str, shown: str | None = None) -> None:
    """Buttons and forms speak for the guest: their choice goes to Gia as an ordinary message,
    so the model still decides which tool to call. `shown` is the short version for the chat."""
    st.session_state.queued = {"text": text, "shown": shown or text}


def say_locally(text: str) -> None:
    """A guest action that needs no model call (e.g. choosing a card) still appears in the chat."""
    st.session_state.messages.append({"role": "user", "content": text, "local": True})


def new_chat() -> None:
    """Fresh conversation. The guest's name and phone are kept so they never have to retype them."""
    st.session_state.agent.reset()
    st.session_state.messages = []
    for k in ("focus_override", "picked", "queued", "manage_mode", "modifying"):
        st.session_state.pop(k, None)


esc = html.escape


def t12(hhmm: str | None) -> str:
    try:
        return human_time(datetime.strptime(hhmm, "%H:%M").time())
    except (TypeError, ValueError):
        return hhmm or ""


def km_from_guest(outlet_id: str, area: str) -> str:
    """'2.4 km from Koramangala' when the guest picked an area, else ''."""
    guest = st.session_state.get("guest_area")
    if not guest or guest == ANYWHERE:
        return ""
    km = catalog.distance_km(catalog.AREA_COORDS[guest], catalog.outlet_coords(outlet_id, area))
    return f"{km:.1f} km from {guest}"


def outlet_map(service: ReservationService, focus_id: str | None, highlight_ids: list[str], height: int = 260) -> None:
    """All outlets as faint pins; suggested ones stronger; the outlet in focus largest; the guest's area in blue."""
    rows = []
    for r in service.db.query("SELECT id, area FROM restaurants"):
        lat, lng = catalog.outlet_coords(r["id"], r["area"])
        if r["id"] == focus_id:
            color, size = "#1E5A47", 420
        elif r["id"] in highlight_ids:
            color, size = "#B4651E", 300
        else:
            color, size = "#9AA59F55", 160
        rows.append({"lat": lat, "lng": lng, "color": color, "size": size})
    guest = st.session_state.get("guest_area")
    if guest and guest != ANYWHERE:
        lat, lng = catalog.AREA_COORDS[guest]
        rows.append({"lat": lat, "lng": lng, "color": "#2B4C7E", "size": 360})
    st.map(pd.DataFrame(rows), latitude="lat", longitude="lng", color="color", size="size", height=height)


def split_name(full: str) -> tuple[str, str]:
    concept, _, area = full.replace("GoodFoods ", "").partition(" – ")
    return concept, area


def outlet_strip(r: dict) -> None:
    concept, area = split_name(r["name"])
    km = km_from_guest(r["id"], area)
    st.markdown(
        f'<div class="gf-focus"><div class="gf-focus-art">{cover_svg(concept, 52, 10)}</div>'
        f'<div><b>{esc(concept)} · {esc(area)}</b><br>'
        f'<span>{esc(r["cuisines"][0])} · ₹{r["price_per_person"]:,} per person · {r["rating"]}★'
        f'{" · " + esc(km) if km else ""}</span></div></div>', unsafe_allow_html=True)


# ----------------------------------------------------------------------------- context from tool results
def _assistant_turns() -> list[dict]:
    return [m for m in st.session_state.messages if m["role"] == "assistant"]


def _rows_from(trace: list) -> list | None:
    rows = None
    for e in trace or []:
        if e.kind == "tool" and not e.detail.get("error"):
            res = e.detail.get("result") or {}
            rows = res.get("recommendations") or res.get("restaurants") or rows
    return rows


def _result(trace: list, statuses: tuple[str, ...]) -> dict | None:
    for e in reversed(trace or []):
        res = e.detail.get("result") or {} if e.kind == "tool" else {}
        if res.get("status") in statuses:
            return res
    return None


def session_context(agent: ReservationAgent) -> dict:
    ctx = {"recs": None, "focus": None, "query": {}, "last_trace": [], "lookup": None, "lookup_phone": None,
           "done": None}
    for m in _assistant_turns():
        for e in m.get("trace") or []:
            if e.kind != "tool" or e.detail.get("error"):
                continue
            args = e.detail.get("arguments") if isinstance(e.detail.get("arguments"), dict) else {}
            res = e.detail.get("result") or {}
            for key in ("date", "time", "party_size"):
                if args.get(key):
                    ctx["query"][key] = args[key]
                if args.get("new_" + key):
                    ctx["query"][key] = args["new_" + key]
            rows = res.get("recommendations") or res.get("restaurants")
            if rows:
                ctx["recs"], ctx["focus"] = rows, rows[0]["restaurant_id"]
            if args.get("restaurant_id"):
                ctx["focus"] = args["restaurant_id"]
            if res.get("restaurant_id"):
                ctx["focus"] = res["restaurant_id"]
            booked = (res.get("summary") or res.get("reservation") or {}) if res.get("status") else {}
            if booked.get("restaurant_id"):
                ctx["focus"] = booked["restaurant_id"]
    if st.session_state.get("focus_override"):
        ctx["focus"] = st.session_state.focus_override
    msgs = st.session_state.messages
    if msgs and msgs[-1]["role"] == "assistant":
        trace = msgs[-1].get("trace") or []
        ctx["last_trace"] = trace
        ctx["done"] = _result(trace, ("confirmed", "modified", "cancelled"))
        for e in trace:
            if e.kind == "tool" and e.name == "lookup_reservation" and not e.detail.get("error"):
                ctx["lookup"] = (e.detail.get("result") or {}).get("reservations", [])
                ctx["lookup_phone"] = (e.detail.get("arguments") or {}).get("phone")
    elif _assistant_turns():
        ctx["last_trace"] = _assistant_turns()[-1].get("trace") or []
    ctx["recs_latest"] = bool(msgs and msgs[-1]["role"] == "assistant" and _rows_from(msgs[-1].get("trace")))
    return ctx


def current_step(agent: ReservationAgent, ctx: dict) -> str:
    """Which controls the chat should offer right now."""
    if agent.pending:
        return "review"
    if ctx["done"]:
        return "done"
    if st.session_state.get("modifying"):
        return "modify"
    if st.session_state.get("manage_mode"):
        return "manage"
    if st.session_state.get("picked"):
        return "details"
    if not st.session_state.messages:
        return "start"
    if ctx["recs_latest"]:
        return "options"
    return "chat"


# ----------------------------------------------------------------------------- step 2: option cards
def _card_html(r: dict) -> str:
    concept, area = split_name(r["name"])
    reason = (r.get("why") or [", ".join(h.replace("_", " ") for h in r.get("highlights", [])[:2])])[0]
    reason = reason[:1].upper() + reason[1:]
    avail = r.get("available_at_requested_time")
    if avail is True:
        tag = '<span class="t">Free at your time</span>'
    elif avail is False and r.get("other_times"):
        tag = f'<span class="t">Free at {esc(", ".join(t12(t) for t in r["other_times"]))}</span>'
    else:
        tag = ""
    km = km_from_guest(r["restaurant_id"], area)
    return (f'{cover_svg(concept, 76, 10)}<b style="margin-top:8px">{esc(concept)}</b>'
            f'<span class="a">{esc(area)} · {esc(r["cuisines"][0])}</span>'
            f'{"<span class=a>" + esc(km) + "</span>" if km else ""}'
            f'<span class="p">₹{r["price_per_person_inr"]:,} · {r["rating"]}★</span>'
            f'<span class="w">{esc(reason)}</span>{tag}')


def pick(r: dict) -> None:
    """Choosing a card opens the booking form straight away — no model call needed for that."""
    concept, area = split_name(r["name"])
    st.session_state.picked = r["restaurant_id"]
    st.session_state.focus_override = r["restaurant_id"]
    st.session_state.pop("manage_mode", None)
    say_locally(f"Chose {concept} – {area}")


def rec_cards(trace: list, interactive: bool, msg_idx: int) -> None:
    rows = _rows_from(trace)
    if not rows:
        return
    rows = rows[:3]
    if not interactive:   # older messages: a static snapshot
        cards = "".join(f'<div class="gf-rec">{_card_html(r)}</div>' for r in rows)
        st.markdown(f'<div class="gf-recs">{cards}</div>', unsafe_allow_html=True)
        return
    picked = st.session_state.get("picked")
    for col, (i, r) in zip(st.columns(len(rows)), enumerate(rows)):
        chosen = r["restaurant_id"] == picked
        with col, st.container(key=f"{'recsel' if chosen else 'rec'}_{msg_idx}_{i}"):
            st.markdown(f'<div class="gf-recbody">{_card_html(r)}</div>', unsafe_allow_html=True)
            st.button("Chosen" if chosen else "Choose", key=f"pick_{msg_idx}_{i}", disabled=chosen,
                      on_click=pick, args=(r,), use_container_width=True)


def raw_trace(trace: list) -> None:
    tools = [e for e in trace if e.kind == "tool"]
    with st.expander(f"Agent trace · {len(tools)} tool call{'s' if len(tools) != 1 else ''} · "
                     f"{sum(e.ms for e in trace) / 1000:.1f}s"):
        for e in trace:
            if e.kind == "tool":
                st.markdown(f"**`{e.name}`** · {e.ms:.0f} ms{' · error' if e.detail.get('error') else ''}")
                st.json({"arguments": e.detail.get("arguments"), "result": e.detail.get("result")}, expanded=False)
            elif e.kind == "guard":
                st.caption(f"Safety check: {e.name.replace('_', ' ')}")
            elif e.name == "error":
                st.caption(f"LLM error: {e.detail.get('message')}")


def chip_row(options: list[tuple[str, object]], key: str) -> None:
    """A row of small action buttons: (label, callback-or-message)."""
    cols = st.columns(len(options))
    for i, (label, action) in enumerate(options):
        if callable(action):
            cols[i].button(label, key=f"{key}{i}", on_click=action, use_container_width=True)
        else:
            cols[i].button(label, key=f"{key}{i}", on_click=queue, args=(action, label), use_container_width=True)


# ----------------------------------------------------------------------------- step 1: start
OCCASIONS = [
    ("Date night", "romantic, rooftop or quiet", "Suggest a place for a date night"),
    ("Family meal", "kid friendly, parking", "Suggest a family-friendly place"),
    ("Birthday or party", "groups, private rooms", "Suggest a place for a birthday party"),
    ("Business lunch", "quiet, Wi-Fi", "Suggest a quiet place for a business lunch"),
    ("Quick bite", "cafés, dosa, biryani", "Suggest a quick, affordable bite"),
    ("Surprise me", "top-rated near me", "Surprise me with a top-rated place"),
]


def step_start() -> None:
    cols = st.columns(3)
    for i, (title, sub, ask) in enumerate(OCCASIONS):
        cols[i % 3].button(f"**{title}** · {sub}", key=f"occ{i}", on_click=queue, args=(ask, title),
                           use_container_width=True)
    st.button("Change or cancel a booking", key="start_manage", on_click=open_manage)


def open_manage() -> None:
    st.session_state.manage_mode = True
    st.session_state.pop("picked", None)
    say_locally("I want to change or cancel a booking")


def leave_manage() -> None:
    for k in ("manage_mode", "modifying"):
        st.session_state.pop(k, None)


def unpick() -> None:
    st.session_state.pop("picked", None)
    st.session_state.pop("focus_override", None)


# ----------------------------------------------------------------------------- step 3: date, guests, time, contact
def _default_day(ctx: dict, options: list[str]) -> str:
    try:
        d = parse_date(ctx["query"]["date"], now().date()).isoformat()
        if d in options:
            return d
    except (KeyError, ParseError, TypeError):
        pass
    return options[0] if now().hour < 21 else options[1]


def _pick_when(service: ReservationService, rid: str, key: str, ctx: dict,
               day_default: str | None = None, party_default: int | None = None,
               time_default: str | None = None) -> tuple[date | None, int, str | None, bool]:
    """Day, guests and time pickers. Only free times are offered. Returns (day, party, hh:mm, any_free)."""
    today = now().date()
    days = flow.day_options(today, 7)
    labels = dict(days) | {"other": "Other date"}
    options = [d for d, _ in days] + ["other"]
    default = day_default if day_default in options else _default_day(ctx, options)
    choice = st.pills("Day", options, format_func=labels.get, default=default, key=f"{key}_day") or default
    if choice == "other":
        day = st.date_input("Pick a date", min_value=today, max_value=today + timedelta(days=60),
                            value=today + timedelta(days=7), key=f"{key}_date", format="DD/MM/YYYY")
    else:
        day = date.fromisoformat(choice)

    try:
        q_party = int(ctx["query"].get("party_size") or 2)
    except (TypeError, ValueError):
        q_party = 2
    party_default = party_default or q_party
    guest_opts = [str(n) for n in range(1, 9)] + ["9+"]
    g_default = str(party_default) if party_default <= 8 else "9+"
    g = st.pills("Guests", guest_opts, default=g_default, key=f"{key}_party") or g_default
    party = int(g) if g != "9+" else st.number_input("How many guests?", 9, 20, max(party_default, 9),
                                                       key=f"{key}_party_n")

    slots = flow.available_slots(service, rid, day, party, now())
    free = [s for s in slots if s.free]
    if not free:
        st.warning(f"No free tables on {day:%a %d %b} for {party}. Try another day or fewer guests, "
                   "or go back and pick a different outlet.")
        return day, party, None, False
    if not time_default:
        try:
            time_default = snap_to_slot(parse_time(ctx["query"]["time"]))[0].strftime("%H:%M")
        except (KeyError, ParseError, TypeError):
            time_default = None
    free_times = [s.hhmm for s in free]
    meals = " & ".join(dict.fromkeys(s.meal for s in free))
    hhmm = st.pills(f"Time · {meals}", free_times, format_func=lambda h: t12(h),
                    default=time_default if time_default in free_times else None,
                    key=f"{key}_time_{day.isoformat()}_{party}")
    full = len(slots) - len(free)
    if full:
        st.caption(f"{full} time{'s' if full != 1 else ''} already full on this day — not shown.")
    return day, party, hhmm, True


def step_details(service: ReservationService, ctx: dict) -> None:
    try:
        r = service.resolve_restaurant(st.session_state.picked)
    except ToolError:
        unpick()
        return
    with st.container(key="flow_box"):
        outlet_strip(r)
        day, party, hhmm, any_free = _pick_when(service, r["id"], f"bk_{r['id']}", ctx)
        c1, c2 = st.columns(2)
        name = c1.text_input("Name", value=st.session_state.get("guest_name", ""), key="f_name")
        phone = c2.text_input("Mobile", value=st.session_state.get("guest_phone", ""), key="f_phone",
                              placeholder="10-digit number")
        request = st.text_input("Special request (optional)", key="f_req",
                                placeholder="High chair, window seat, birthday cake…")
        b1, b2 = st.columns([2, 1])
        label = f"Review booking · {day:%a %d %b}, {t12(hhmm)}, {party} guests" if hhmm else "Pick a time first"
        if b1.button(label, type="primary", disabled=not (any_free and hhmm), use_container_width=True,
                     key="f_review"):
            try:
                clean_name, clean_phone = validate_name(name), normalize_phone(phone)
            except ToolError as exc:
                st.error(exc.message)
            else:
                st.session_state.guest_name, st.session_state.guest_phone = clean_name, clean_phone[-10:]
                queue(*flow.booking_request(r, day, hhmm, party, clean_name, clean_phone, request.strip() or None))
                st.rerun()
        b2.button("Back to options", on_click=unpick, use_container_width=True, key="f_back")


# ----------------------------------------------------------------------------- step 4: review & confirm
def drop_pending(note: str) -> None:
    """'Edit' or 'Keep my booking': forget the preview; nothing was written."""
    st.session_state.agent.pending = None
    say_locally(note)


def step_review(service: ReservationService, agent: ReservationAgent) -> None:
    tool, s = agent.pending.tool, agent.pending.summary
    data = s.get("after") or s.get("reservation") or s
    concept, area = split_name(data.get("restaurant", ""))
    title, action, back, back_note = {
        "make_reservation": ("Review your booking", "Confirm booking", "Edit details", "Let me edit the details"),
        "modify_reservation": ("Review the change", "Confirm change", "Edit change", "Let me edit the change"),
        "cancel_reservation": ("Cancel this booking?", "Yes, cancel it", "Keep my booking", "Keep my booking"),
    }.get(tool, ("Review", "Confirm", "Back", "Go back"))
    rows = [("When", f"{data.get('day', '')}, {t12(data.get('time'))}"), ("Guests", data.get("party_size")),
            ("Seating", data.get("seating")), ("Name", data.get("name")), ("Mobile", data.get("phone")),
            ("Request", data.get("special_requests"))]
    if tool == "modify_reservation" and s.get("before"):
        b = s["before"]
        rows.insert(0, ("Was", f"{b.get('day', '')}, {t12(b.get('time'))}, {b.get('party_size')} guests"))
    kv = "".join(f'<div class="k">{k}</div><div class="v">{esc(str(v))}</div>' for k, v in rows if v)
    with st.container(key="review_card"):
        st.markdown(f'{cover_svg(concept, 100, 12)}<div class="gf-label" style="margin-top:14px">{title}</div>'
                    f'<div class="gf-title">{esc(concept)} · {esc(area)}</div><div class="gf-kv" '
                    f'style="margin-top:12px">{kv}</div>', unsafe_allow_html=True)
        c1, c2 = st.columns([2, 1])
        c1.button(action, type="primary", use_container_width=True, on_click=queue, args=("Yes, confirm", action),
                  key="rv_yes")
        c2.button(back, use_container_width=True, on_click=drop_pending, args=(back_note,), key="rv_back")
        st.markdown('<div class="gf-note">Checked against live tables · nothing changes until you confirm</div>',
                    unsafe_allow_html=True)


# ----------------------------------------------------------------------------- step 5: booked
def change_from_ticket(code: str) -> None:
    st.session_state.manage_mode = True
    phone = st.session_state.get("guest_phone")
    if phone:
        queue(f"Find my booking {code}. Phone: {phone}.", f"Change booking {code}")


def cancel_from_ticket(code: str) -> None:
    phone = st.session_state.get("guest_phone")
    st.session_state.manage_mode = True
    if phone:
        queue(f"Cancel booking {code}. Phone: {phone}.", f"Cancel {code}")


def step_done(service: ReservationService, res: dict) -> None:
    data = res.get("summary") or res.get("reservation") or {}
    code = res.get("confirmation_code") or data.get("confirmation_code", "")
    try:
        r = service.resolve_restaurant(data.get("restaurant_id"))
    except ToolError:
        return
    concept, area = split_name(r["name"])
    with st.container(key="ticket"):
        if res["status"] == "cancelled":
            st.markdown(f'<div class="gf-label">Cancelled</div><div class="gf-title">{esc(concept)} · {esc(area)}</div>'
                        f'<div class="gf-meta">{esc(data.get("day", ""))}, {t12(data.get("time"))} · '
                        f'booking {esc(code)} is cancelled.</div>', unsafe_allow_html=True)
        else:
            verb = "Confirmed" if res["status"] == "confirmed" else "Updated"
            st.markdown(
                f'<div class="gf-ticket-head"><span class="gf-check">✓</span><div><div class="gf-label" '
                f'style="margin:0">{verb}</div><div class="gf-title">{esc(concept)} · {esc(area)}</div></div></div>'
                f'<div class="gf-ticket-grid"><div><span>Day</span><b>{esc(data.get("day", ""))}</b></div>'
                f'<div><span>Time</span><b>{t12(data.get("time"))}</b></div>'
                f'<div><span>Guests</span><b>{data.get("party_size", "")}</b></div></div>'
                '<div class="gf-label" style="margin:14px 0 4px">Confirmation code</div>', unsafe_allow_html=True)
            st.code(code, language=None)
            c1, c2 = st.columns(2)
            c1.download_button("Add to calendar", flow.calendar_file(
                code, r["name"], r["address"], data.get("date", ""), data.get("time", "20:00"),
                int(data.get("party_size") or 2), now()), file_name=f"{code}.ics", mime="text/calendar",
                use_container_width=True, key="tk_cal")
            c2.link_button(f"Directions · {area}", flow.directions_url(r["id"], area), use_container_width=True)
    if res["status"] == "cancelled":
        chip_row([("Book another table", new_chat)], "done")
    else:
        chip_row([("Change this booking", lambda c=code: change_from_ticket(c)),
                  ("Cancel it", lambda c=code: cancel_from_ticket(c)),
                  ("Book another table", new_chat)], "done")


# ----------------------------------------------------------------------------- step 6: change or cancel
def step_manage(ctx: dict) -> None:
    phone = ctx["lookup_phone"] or st.session_state.get("manage_phone") or st.session_state.get("guest_phone", "")
    if ctx["lookup"]:
        for i, b in enumerate(ctx["lookup"]):
            concept, area = split_name(b.get("restaurant", ""))
            with st.container(key=f"bk_card_{i}"):
                st.markdown(f'<b>{esc(concept)} · {esc(area)}</b> <span class="gf-meta">· {esc(b["confirmation_code"])}'
                            f'</span><div class="gf-meta">{esc(b.get("day", ""))} · {t12(b.get("time"))} · '
                            f'{b.get("party_size")} guests</div>', unsafe_allow_html=True)
                if "*" in b["confirmation_code"]:
                    # Found by phone only: the code is masked, so ask for it before allowing changes.
                    u1, u2 = st.columns([2, 1])
                    typed = u1.text_input("Confirmation code", key=f"unlock_code_{i}", placeholder="GF-XXXXXX",
                                          label_visibility="collapsed")
                    if u2.button("Unlock", key=f"unlock_{i}", use_container_width=True):
                        typed = typed.strip().upper()
                        if not CODE_RE.fullmatch(typed):
                            st.error("Enter the full code from your confirmation, like GF-AB12CD.")
                        else:
                            queue(f"Find my booking {typed}. Phone: {phone}.", f"Find booking {typed}")
                            st.rerun()
                    st.markdown('<div class="gf-note">Enter the code from your confirmation to change or cancel '
                                'this booking.</div>', unsafe_allow_html=True)
                    continue
                c1, c2 = st.columns(2)
                c1.button("Change day, time or guests", key=f"mod_{i}", use_container_width=True,
                          on_click=lambda bk=b, ph=phone: st.session_state.update(modifying={**bk, "phone": ph}))
                c2.button("Cancel booking", key=f"can_{i}", use_container_width=True, on_click=queue,
                          args=(f"Cancel booking {b['confirmation_code']}. Phone: {phone}.",
                                f"Cancel {b['confirmation_code']}"))
        chip_row([("Done", leave_manage)], "mg")
        return
    with st.container(key="flow_box"):
        st.markdown('<div class="gf-label">Find your booking</div>', unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        ph = c1.text_input("Mobile you booked with", value=phone, key="m_phone", placeholder="10-digit number")
        code = c2.text_input("Code (optional)", key="m_code", placeholder="GF-XXXXXX")
        b1, b2 = st.columns([2, 1])
        if b1.button("Find my bookings", type="primary", use_container_width=True, key="m_find"):
            try:
                clean = normalize_phone(ph)[-10:]
            except ToolError as exc:
                st.error(exc.message)
            else:
                st.session_state.manage_phone = clean
                code = code.strip().upper()
                queue(f"Find my upcoming bookings. Phone: {clean}." + (f" Code: {code}." if code else ""),
                      "Find my bookings" + (f" · {code}" if code else ""))
                st.rerun()
        b2.button("Back", on_click=leave_manage, use_container_width=True, key="m_back")


def step_modify(service: ReservationService, ctx: dict) -> None:
    b = st.session_state.modifying
    try:
        r = service.resolve_restaurant(b["restaurant_id"])
    except ToolError:
        st.session_state.pop("modifying", None)
        return
    with st.container(key="flow_box"):
        outlet_strip(r)
        st.markdown(f'<div class="gf-meta">Now: {esc(b.get("day", ""))} · {t12(b.get("time"))} · '
                    f'{b.get("party_size")} guests · {esc(b["confirmation_code"])}</div>', unsafe_allow_html=True)
        day, party, hhmm, any_free = _pick_when(service, r["id"], f"mod_{b['confirmation_code']}", ctx,
                                                day_default=b.get("date"), party_default=b.get("party_size"),
                                                time_default=b.get("time"))
        b1, b2 = st.columns([2, 1])
        if b1.button("Review change", type="primary", disabled=not (any_free and hhmm), use_container_width=True,
                     key="mod_review"):
            queue(*flow.change_request(b["confirmation_code"], b.get("phone", ""), day, hhmm, party))
            st.rerun()
        b2.button("Back", on_click=lambda: st.session_state.pop("modifying", None), use_container_width=True,
                  key="mod_back")


def action_area(agent: ReservationAgent, service: ReservationService, ctx: dict) -> None:
    """The controls under the conversation change with the step the guest is on."""
    step = current_step(agent, ctx)
    if step == "start":
        step_start()
    elif step == "options":
        chip_row([("Cheaper", "Show me cheaper options"), ("Top rated", "Show the top-rated options"),
                  ("Another area", "Show options in a nearby area"), ("Different cuisine", "Show a different cuisine")],
                 "ref")
    elif step == "details":
        step_details(service, ctx)
    elif step == "review":
        step_review(service, agent)
    elif step == "done":
        step_done(service, ctx["done"])
    elif step == "manage":
        step_manage(ctx)
    elif step == "modify":
        step_modify(service, ctx)
    else:
        chip_row([("Suggest a place", "Suggest a good place for me"), ("Change or cancel", open_manage),
                  ("Start over", new_chat)], "chat")


# ----------------------------------------------------------------------------- right-hand panel
def outlet_panel(service: ReservationService, ctx: dict) -> None:
    try:
        r = service.resolve_restaurant(ctx["focus"])
    except ToolError:
        return
    concept, area = split_name(r["name"])
    pills = "".join(f'<span class="gf-pill">{esc(f.replace("_", " "))}</span>' for f in r["features"][:4])
    km = km_from_guest(r["id"], area)
    dishes = ", ".join(r["signature_dishes"])
    with st.container(key="ctx_outlet"):
        st.markdown(
            f'{cover_svg(concept, 120, 14, r["name"])}'
            f'<div class="gf-label" style="margin-top:16px">Outlet in focus</div>'
            f'<div class="gf-title">{esc(concept)} · {esc(area)}</div>'
            f'<div class="gf-meta">{esc(", ".join(r["cuisines"]))} · {r["total_seats"]} seats · '
            f'₹{r["price_per_person"]:,} per person · {r["rating"]}★</div>'
            f'<div class="gf-meta">Known for {esc(dishes)}</div>'
            f'<div class="gf-meta">Open {r["open_time"]}–{r["close_time"]} · {esc(r["address"])}'
            f'{" · <b>" + esc(km) + "</b>" if km else ""}</div>'
            f'<div class="gf-pills">{pills}</div>', unsafe_allow_html=True)


def step_line(e) -> str | None:
    if e.kind == "guard":
        label = {"confirmation_required": "Asked for your confirmation", "write_authorised": "Your yes was verified",
                 "hallucinated_code_blocked": "Blocked an unverified code"}.get(e.name)
        return f'<div class="gf-step"><span class="gf-dot" style="background:#4A5650"></span><span class="n">{label}</span></div>' if label else None
    if e.kind != "tool":
        return None
    r = e.detail.get("result") or {}
    color, note = "#2F8F5B", ""
    if e.detail.get("error"):
        color, note = "#B42318", str(r.get("error", "error")).replace("_", " ").lower()
    elif r.get("status") == "needs_user_confirmation":
        color, note = "#B4651E", "preview · awaiting your yes"
    elif e.name == "recommend_restaurants":
        note = f"{len(r.get('recommendations', []))} picks" + (f" · {r['occasion_detected']}" if r.get("occasion_detected") else "")
    elif e.name == "search_restaurants":
        note = f"{r.get('count', 0)} matches"
    elif e.name == "check_availability":
        note = f"{t12(r.get('time'))} free" if r.get("available") else "full · alternatives offered"
    elif e.name == "lookup_reservation":
        note = f"{len(r.get('reservations', []))} found"
    elif r.get("status"):
        note = r["status"] + (f" · {r['confirmation_code']}" if r.get("confirmation_code") else "")
    else:
        note = "details loaded"
    return (f'<div class="gf-step"><span class="gf-dot" style="background:{color}"></span>'
            f'<code>{esc(e.name)}</code><span class="n">{esc(note)}</span></div>')


def steps_panel(ctx: dict) -> None:
    lines = [x for x in (step_line(e) for e in ctx["last_trace"]) if x]
    if not lines:
        return
    with st.container(key="ctx_steps"):
        st.markdown('<div class="gf-label">What Gia just did</div>' + "".join(lines), unsafe_allow_html=True)


def intro_panel(service: ReservationService) -> None:
    rows = service.db.query("SELECT COUNT(*) n, SUM(total_seats) s, COUNT(DISTINCT area) a FROM restaurants")[0]
    with st.container(key="ctx_intro"):
        st.markdown(
            '<div class="gf-label">How Gia helps</div>'
            '<div class="gf-stat"><span>Suggests the right outlet</span><b>with reasons</b></div>'
            '<div class="gf-stat"><span>Shows only free tables</span><b>live</b></div>'
            '<div class="gf-stat"><span>Books, changes, cancels</span><b>only on your confirm</b></div>'
            '<div class="gf-label" style="margin-top:22px">The network</div>'
            f'<div class="gf-stat"><span>Outlets</span><b>{rows["n"]}</b></div>'
            f'<div class="gf-stat"><span>Neighbourhoods</span><b>{rows["a"]}</b></div>'
            f'<div class="gf-stat"><span>Seats</span><b>{rows["s"]:,}</b></div>', unsafe_allow_html=True)


# ----------------------------------------------------------------------------- pages
def page_chat(agent: ReservationAgent, service: ReservationService, show_trace: bool) -> None:
    ctx = session_context(agent)
    left, right = st.columns([1.55, 1], gap="large")

    with left:
        with st.container(key="chat_card"):
            hour = now().hour
            greet = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"
            h, b = st.columns([5, 1.2], vertical_alignment="center")
            h.markdown(f'<div class="gf-h1">{greet}. Where shall we eat?</div>'
                       f'<div class="gf-sub">{service.db.restaurant_count()} outlets across Bengaluru · '
                       'English, Hindi or Hinglish</div>', unsafe_allow_html=True)
            if st.session_state.messages:
                b.button("New chat", on_click=new_chat, use_container_width=True)
            near, _ = st.columns([1.3, 2])
            area = near.selectbox("I'm near", [ANYWHERE] + catalog.AREAS, key="guest_area",
                                  help="Gia will suggest outlets in and around this area.")
            agent.guest_area = None if area == ANYWHERE else area
            st.divider()

            if not st.session_state.messages:
                with st.chat_message("assistant", avatar=AVATARS["assistant"]):
                    st.markdown("Hi, I'm **Gia**. What's the occasion? Pick one to start, or just type.")
            msgs = st.session_state.messages
            last_idx = len(msgs) - 1
            for idx, m in enumerate(msgs):
                with st.chat_message(m["role"], avatar=AVATARS[m["role"]]):
                    st.markdown(m["content"])
                    if m["role"] == "assistant" and m.get("trace"):
                        for e in m["trace"]:
                            if e.kind == "llm" and e.name == "error":
                                st.caption(f"Technical detail: {e.detail.get('message')} — run "
                                           "`python -m goodfoods.diagnose` to test the connection.")
                        live = idx == last_idx or (idx == last_idx - 1 and msgs[-1].get("local"))
                        rec_cards(m["trace"], interactive=live, msg_idx=idx)
                        if show_trace:
                            raw_trace(m["trace"])
            pending_area = st.container()

            action_area(agent, service, ctx)
            typed = st.chat_input("Or just type to Gia…")
            q = {"text": typed, "shown": typed} if typed else st.session_state.pop("queued", None)

    if q:
        with pending_area:
            with st.chat_message("user", avatar=AVATARS["user"]):
                st.markdown(q["shown"])
            with st.chat_message("assistant", avatar=AVATARS["assistant"]):
                waiting = st.empty()
                agent.llm.on_wait = lambda secs: waiting.info(
                    f"Lots of requests right now — waiting {secs:.0f}s for the model, then continuing…")
                with st.spinner("Checking tables…"):
                    turn = agent.chat(q["text"])
                waiting.empty()
        st.session_state.messages += [{"role": "user", "content": q["shown"]},
                                      {"role": "assistant", "content": turn.reply, "trace": turn.trace}]
        u = st.session_state.usage
        u["prompt_tokens"] += turn.usage.get("prompt_tokens", 0)
        u["completion_tokens"] += turn.usage.get("completion_tokens", 0)
        u["turns"] += 1
        u["llm_ms"] += sum(e.ms for e in turn.trace if e.kind == "llm")
        st.session_state.pop("focus_override", None)   # Gia's tool calls now say which outlet is in focus
        if _rows_from(turn.trace):
            st.session_state.pop("picked", None)          # fresh options: nothing chosen yet
        if _result(turn.trace, ("confirmed", "modified", "cancelled")):
            for k in ("picked", "manage_mode", "modifying"):
                st.session_state.pop(k, None)
        if any(e.kind == "tool" and e.name == "lookup_reservation" for e in turn.trace):
            st.session_state.manage_mode = True
            st.session_state.pop("modifying", None)
        st.rerun()   # redraw the step controls and the context panel from the new state

    with right, st.container(key="ctx_col"):
        if not ctx["focus"]:
            intro_panel(service)
        else:
            outlet_panel(service, ctx)
            steps_panel(ctx)
        with st.container(key="ctx_map"):
            st.markdown('<div class="gf-label">On the map</div>', unsafe_allow_html=True)
            outlet_map(service, ctx["focus"], [r["restaurant_id"] for r in (ctx["recs"] or [])][:3])
            st.markdown('<div class="gf-legend"><span><i style="background:#1E5A47"></i>In focus</span>'
                        '<span><i style="background:#B4651E"></i>Suggested</span>'
                        '<span><i style="background:#2B4C7E"></i>You</span>'
                        '<span><i style="background:#9AA59F"></i>Other outlets</span></div>',
                        unsafe_allow_html=True)


def page_explore(service: ReservationService) -> None:
    st.markdown('<div class="gf-h1">Explore outlets</div><div class="gf-sub">Every GoodFoods kitchen, '
                'filterable. Ask Gia to book any of them.</div>', unsafe_allow_html=True)
    rows = service.db.query("SELECT * FROM restaurants ORDER BY rating DESC")
    c1, c2, c3 = st.columns(3)
    c1.metric("Outlets", len(rows))
    c2.metric("Neighbourhoods", len({r["area"] for r in rows}))
    c3.metric("Seats", f"{sum(r['total_seats'] for r in rows):,}")
    f1, f2, f3 = st.columns([1.2, 1.2, 1])
    areas = f1.multiselect("Area", catalog.AREAS)
    cuisines = f2.multiselect("Cuisine", sorted({c for r in rows for c in r["cuisines"]}))
    max_price = f3.slider("Max ₹ per person", 200, 3000, 3000, step=100)
    feats = st.multiselect("Must have", catalog.FEATURES, format_func=lambda f: f.replace("_", " "))
    table = [{
        "ID": r["id"], "Outlet": r["name"].replace("GoodFoods ", ""), "Area": r["area"],
        "Cuisine": ", ".join(r["cuisines"]), "₹/person": r["price_per_person"], "Rating": r["rating"],
        "Seats": r["total_seats"], "Private room": r["private_dining_capacity"] or "—",
        "Hours": f"{r['open_time']}–{r['close_time']}",
        "Features": ", ".join(f.replace("_", " ") for f in r["features"]),
    } for r in rows
        if (not areas or r["area"] in areas) and (not cuisines or set(cuisines) & set(r["cuisines"]))
        and r["price_per_person"] <= max_price and all(f in r["features"] for f in feats)]
    st.caption(f"{len(table)} outlets match · select a row to book it with Gia")
    event = st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True,
                         column_config={"Rating": st.column_config.NumberColumn(format="%.1f ★")},
                         on_select="rerun", selection_mode="single-row", key="explore_table")
    picked_rows = event.selection.rows if event and event.selection else []
    selected = table[picked_rows[0]] if picked_rows and picked_rows[0] < len(table) else None
    filtered = len(table) < len(rows)
    outlet_map(service, selected["ID"] if selected else None,
               [t["ID"] for t in table] if filtered else [], height=320)
    if selected:
        row = selected
        c1, c2 = st.columns([3, 1], vertical_alignment="center")
        c1.markdown(f"**{esc(row['Outlet'])}** · {esc(row['Cuisine'])} · ₹{row['₹/person']:,} per person")
        c2.button("Book with Gia", type="primary", use_container_width=True, on_click=book_from_explore,
                  args=(row["ID"], row["Outlet"]))


def book_from_explore(rid: str, outlet: str) -> None:
    """Jump to the chat with the booking form for this outlet already open."""
    st.session_state.nav = PAGES[0]
    st.session_state.picked = rid
    st.session_state.focus_override = rid
    st.session_state.pop("manage_mode", None)
    say_locally(f"Chose {outlet}")


def page_bookings(service: ReservationService) -> None:
    st.markdown('<div class="gf-h1">Bookings &amp; metrics</div><div class="gf-sub">Demand across the '
                'network and what the agent has done this session.</div>', unsafe_allow_html=True)
    u = st.session_state.usage
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Turns this session", u["turns"])
    c2.metric("Tokens in / out", f"{u['prompt_tokens']:,} / {u['completion_tokens']:,}")
    c3.metric("Avg LLM time per turn", f"{(u['llm_ms'] / u['turns'] / 1000) if u['turns'] else 0:.1f}s")
    stats = service.db.query("SELECT status, COUNT(*) n FROM reservations WHERE source != 'seed' GROUP BY status")
    c4.metric("Agent bookings (active)", next((s["n"] for s in stats if s["status"] == "confirmed"), 0))

    daily = service.db.query("SELECT res_date, SUM(party_size) covers FROM reservations WHERE status = 'confirmed' "
                             "AND res_date >= ? GROUP BY res_date ORDER BY res_date LIMIT 14",
                             (now().date().isoformat(),))
    if daily:
        st.markdown('<div class="gf-label" style="margin-top:18px">Covers booked by day</div>', unsafe_allow_html=True)
        df = pd.DataFrame([{"Day": datetime.fromisoformat(d["res_date"]).strftime("%m-%d %a"), "Covers": d["covers"]}
                           for d in daily])
        st.bar_chart(df, x="Day", y="Covers", color="#1E5A47", height=260)

    st.markdown('<div class="gf-label" style="margin-top:18px">Made through Gia</div>', unsafe_allow_html=True)
    data = service.upcoming_reservations()
    if data:
        st.dataframe(pd.DataFrame(data).rename(columns={
            "code": "Code", "restaurant": "Outlet", "res_date": "Date", "res_time": "Time", "party_size": "Guests",
            "customer_name": "Name", "status": "Status", "source": "Channel", "created_at": "Created"}),
            hide_index=True, use_container_width=True)
    else:
        st.info("No bookings made through Gia yet.")


# ----------------------------------------------------------------------------- layout
settings = load_llm_settings()
agent = get_agent()
service = get_service()

with st.sidebar:
    st.markdown("**Settings**")
    show_trace = st.toggle("Show raw agent trace in chat", value=False)
    st.caption("Model")
    st.code(f"{settings.model}\n{settings.base_url}", language=None)
    st.caption("Tools are served over MCP (JSON-RPC 2.0). Bookings, changes and cancellations always "
               "need an explicit yes.")

if "nav" not in st.session_state:
    st.session_state.nav = PAGES[0]
c1, c2, c3 = st.columns([1.1, 2.2, 1.5], vertical_alignment="center")
c1.markdown('<div class="gf-wordmark">GoodFoods</div>', unsafe_allow_html=True)
with c2:
    page = st.segmented_control("Navigate", PAGES, key="nav", label_visibility="collapsed") or PAGES[0]
has_key = bool(settings.api_key) or "localhost" in settings.base_url or "127.0.0.1" in settings.base_url
c3.markdown(
    f'<div class="gf-status"><span class="gf-dot" style="background:{"#2F8F5B" if has_key else "#B4651E"}"></span>'
    f'{esc(settings.model) + " · MCP tools live" if has_key else "Add LLM_API_KEY to .env"}</div>',
    unsafe_allow_html=True)
st.write("")

if page == PAGES[0]:
    page_chat(agent, service, show_trace)
elif page == PAGES[1]:
    page_explore(service)
else:
    page_bookings(service)
