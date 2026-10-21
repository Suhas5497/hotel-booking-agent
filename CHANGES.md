# Changes from goodfoods-agent

## New: voice

- **Talk to Gia.** A Voice switch and a **Talk to Gia** button. Gia greets you aloud, listens through the
  mic (Whisper, English and Hindi) and reads her replies aloud (the browser's own speech engine, no key).
- **Help me choose.** A button under the options, plus a prompt rule: when you can't decide, Gia picks one
  of the options she showed and says why it suits your occasion. For surprises and celebrations she
  offers to add a special request (cake, flowers, a quiet table).
- **Option cards stay selectable** while you ask about them. Before, any reply without new cards
  (such as "which one is best?") turned the cards into a static picture, so you couldn't pick the one
  Gia suggested.
- After you choose a card, Gia tells you the next step: choose your day, guests and time, then
  enter your details.
- Tests: `tests/test_voice.py` (11 tests) and a voice journey in `tests/ui_walkthrough.py` that goes
  greeting, speak your request, options read aloud, help me choose, pick a card.

## Fixes

This repository starts from [goodfoods-agent](https://github.com/Suhas5497/goodfoods-agent) at commit
`e9d0f6f` and fixes the problems found in a code review. Every fix below has a regression test in
`tests/test_fixes.py`; 14 of those tests fail on the original code.

### Safety

**The confirmation could approve a different phone number.**
The approval key was a hash of the preview summary, which only shows the masked phone
(`+91 ******3210`). A guest could confirm a booking for one number, and if the model then sent a
different number with the same last four digits, the keys matched and the booking was saved.
The key now also covers the full, normalised phone and confirmation code (`agent.py`, `_gated_write`).
The same number written differently (`+91 98765 43210`) still matches.

**An approval could carry over to a later turn.**
If the guest said yes but the model didn't re-issue the write in that turn, the approval stayed
valid and could authorise a write in a later turn without asking again. Approvals now only last
for the turn in which the guest gave them.

**A phone number alone gave full control of someone's booking.**
`lookup_reservation(phone)` returned the confirmation code, and code + phone is all that change and
cancel need. So anyone who knew a guest's number could cancel their booking.
- Searching by phone alone now returns masked codes (`GF-****23`). Masked codes don't match the code
  format, so they can't be passed back to change or cancel.
- Changing or cancelling needs the full code from the confirmation.
- Five wrong codes for one number pause lookups on it for 15 minutes, so codes can't be guessed.
- The UI asks for the code on a masked booking card ("Unlock") before showing change and cancel.
  "Change this booking" on the confirmation ticket still works in one tap, since it has the code.

### Correctness

**"Non-vegetarian" returned only pure-veg restaurants.**
`"vegetarian" in "non-vegetarian"` is true, so the veg-only filter kicked in. Veg detection now
removes "non-veg" / "non-vegetarian" first and matches whole words (`services.py`, `wants_veg_only`).

**Back-to-back sittings made tables look full.**
Capacity counted every booking that overlapped the requested 90 minutes. A 6:30 and an 8:00 sitting
don't overlap each other and can use the same table, but both overlap a 7:30 request, so they were
counted as two tables. Capacity now uses the peak number of tables in use at the same moment. It
still never overbooks; it just stops turning guests away when a table is actually free.

### Robustness

**Booking details got lost in longer chats.**
History was cut at 16 messages, and tool calls count as messages, so a name or phone given early
could drop out after two or three turns. The agent now keeps the details the guest has already
given (outlet, date, time, party size, name, phone, code) in working memory and restates them in
the system prompt. History is trimmed by whole guest turns (the last six), so a tool result is never
separated from the call that produced it.

**Long rate-limit waits froze the app.**
A turn could sleep for up to about 75 seconds (25 seconds × 3 retries), and when the provider asked
for a longer wait the loop fell through to normal backoff and kept retrying anyway. Waiting is now
budgeted at 25 seconds per turn in total, and a longer requested wait fails at once with a clear
message.

**Smaller fixes**
- Internal correction notes ("SYSTEM CHECK: …") are flagged in code instead of recognised by their
  text, so a guest typing that phrase no longer has their message deleted. The flag is stripped
  before anything is sent to the model API.
- `reset()` (New chat) now also forgets known confirmation codes and remembered details.
- The MCP server no longer echoes internal exception text to clients; details go to the server log.
- "8 baje" and "8 o'clock" are now understood as times (read against the outlet's hours, or asked
  about if both 8 AM and 8 PM are possible).

### Tests

- `tests/test_fixes.py`: 19 new tests, one or more per fix.
- `tests/ui_walkthrough.py`: now also checks that a phone-only lookup hides change/cancel and that
  unlocking with the code brings them back.
- Total: 85 offline tests (was 55), all passing.
