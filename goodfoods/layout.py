"""One-screen chat layout helpers (no Streamlit import, so they're testable).

The chat column and the booking panel are Streamlit containers with a fixed height that
scroll inside themselves. `fit_script` returns a tiny script, run in a zero-height
component, that:

* sizes the chat box so the whole chat card (Gia, messages, chips, mic and input) fits the
  window, and the booking panel so it fills the rest of the window height;
* scrolls the chat box to the newest message when a message has been added (and only then,
  so a guest reading older messages isn't yanked to the bottom by an unrelated click);
* re-fits when the window is resized.

It finds the boxes through marker elements (`.gia-chat-end`, `.gia-side-end`) placed at the
end of each box, then walks up to the nearest scrolling ancestor, so it doesn't depend on
Streamlit's internal class names.
"""
from __future__ import annotations

import json

CHAT_END = '<div class="gia-chat-end"></div>'
SIDE_END = '<div class="gia-side-end"></div>'
CHAT_HEIGHT = 300   # the height given to st.container; the script recognises the box by it
SIDE_HEIGHT = 600


def fit_script(message_count: int, force_scroll: bool = False, nonce: str = "") -> str:
    payload = json.dumps({"count": message_count, "force": force_scroll, "nonce": nonce,
                          "chatH": CHAT_HEIGHT, "sideH": SIDE_HEIGHT})
    return """<script>
(function () {
  var p = %s;
  var W = window;
  try { if (window.parent && window.parent.document) W = window.parent; } catch (e) {}
  var D = W.document;
  // Streamlit wraps a fixed-height container in more than one element: the one carrying the
  // height and the one that scrolls can differ. Walk up from the marker, never past `boundary`
  // (the chat card / side column), and size every element that scrolls, has an inline pixel
  // height, or has exactly the height we gave the container (`given`) or one we set earlier.
  function sized(marker, boundary, given) {
    var out = [], el = marker && marker.parentElement;
    while (el && el !== D.body && !(boundary && (el === boundary || el.contains(boundary)))) {
      var cs = W.getComputedStyle(el), oy = cs.overflowY, h = parseFloat(cs.height);
      var fixed = /px$/.test(el.style.height || "") || Math.abs(h - given) < 2 || el.hasAttribute("data-gia-sized");
      if (fixed || oy === "auto" || oy === "scroll") { out.push(el); el.setAttribute("data-gia-sized", "1"); }
      el = el.parentElement;
    }
    return out;
  }
  function setH(list, h) {
    list.forEach(function (el) { el.style.height = h + "px"; el.style.maxHeight = h + "px"; });
  }
  function toBottom(list) { list.forEach(function (el) { el.scrollTop = el.scrollHeight; }); }
  function fit(scrollToEnd) {
    var narrow = W.innerWidth < 760, vh = W.innerHeight;
    var card = D.querySelector(".st-key-chat_card");
    var chat = sized(D.querySelector(".gia-chat-end"), card, p.chatH);
    if (chat.length) {
      var outer = chat[chat.length - 1];
      var below = card ? Math.max(0, card.getBoundingClientRect().bottom - outer.getBoundingClientRect().bottom) : 180;
      var h = narrow ? Math.round(vh * 0.55)
                     : Math.round(vh - outer.getBoundingClientRect().top - below - 24);
      setH(chat, Math.max(narrow ? 240 : 220, h));
      if (scrollToEnd) toBottom(chat);
    }
    var side = sized(D.querySelector(".gia-side-end"), D.querySelector(".st-key-ctx_col"), p.sideH);
    if (side.length) {
      if (narrow) side.forEach(function (el) { el.style.height = ""; el.style.maxHeight = ""; });
      else {
        var top = side[side.length - 1].getBoundingClientRect().top;
        setH(side, Math.max(300, Math.round(vh - top - 24)));
      }
    }
  }
  var changed = p.force || W.__giaMsgCount !== p.count;
  W.__giaMsgCount = p.count;
  [0, 80, 250, 600, 1200, 2000].forEach(function (t) { setTimeout(function () { fit(changed); }, t); });
  W.__giaFit = fit;
  if (!W.__giaFitBound) {
    W.__giaFitBound = true;
    W.addEventListener("resize", function () { if (W.__giaFit) W.__giaFit(false); });
  }
})();
</script>""" % payload.replace("<", "\\u003c")
