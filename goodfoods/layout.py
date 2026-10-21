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


def fit_script(message_count: int, force_scroll: bool = False, nonce: str = "") -> str:
    payload = json.dumps({"count": message_count, "force": force_scroll, "nonce": nonce})
    return """<script>
(function () {
  var p = %s;
  var W = window;
  try { if (window.parent && window.parent.document) W = window.parent; } catch (e) {}
  var D = W.document;
  function scroller(el) {
    while (el && el !== D.body) {
      var oy = W.getComputedStyle(el).overflowY;
      if ((oy === "auto" || oy === "scroll") && el.clientHeight > 0) return el;
      el = el.parentElement;
    }
    return null;
  }
  function fit(scrollToEnd) {
    var narrow = W.innerWidth < 760, vh = W.innerHeight;
    var end = D.querySelector(".gia-chat-end"), card = D.querySelector(".st-key-chat_card");
    var box = end && scroller(end);
    if (box) {
      var below = card ? Math.max(0, card.getBoundingClientRect().bottom - box.getBoundingClientRect().bottom) : 180;
      var h = narrow ? Math.round(vh * 0.58) : Math.round(vh - box.getBoundingClientRect().top - below - 20);
      h = Math.max(narrow ? 260 : 300, h);
      box.style.height = h + "px"; box.style.maxHeight = h + "px";
      if (scrollToEnd) box.scrollTop = box.scrollHeight;
    }
    var side = D.querySelector(".gia-side-end"), sbox = side && scroller(side);
    if (sbox) {
      if (narrow) { sbox.style.height = ""; sbox.style.maxHeight = ""; }
      else {
        var h2 = Math.max(320, Math.round(vh - sbox.getBoundingClientRect().top - 20));
        sbox.style.height = h2 + "px"; sbox.style.maxHeight = h2 + "px";
      }
    }
  }
  var changed = p.force || W.__giaMsgCount !== p.count;
  W.__giaMsgCount = p.count;
  [0, 80, 250, 600, 1200].forEach(function (t) { setTimeout(function () { fit(changed); }, t); });
  W.__giaFit = fit;
  if (!W.__giaFitBound) {
    W.__giaFitBound = true;
    W.addEventListener("resize", function () { if (W.__giaFit) W.__giaFit(false); });
  }
})();
</script>""" % payload.replace("<", "\\u003c")
