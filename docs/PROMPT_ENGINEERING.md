# Prompt engineering notes

This covers how I prompt the model, what I deliberately keep out of the prompt, and what changed after testing with a real model.

## The model

The brief asks for Llama 3.x 8B or a similar small model. I started with Llama 3.1 8B on Groq. Groq retired it in August 2026, so the default is now `openai/gpt-oss-20b`, a small open-weight model Groq suggests as the replacement. The same prompt runs on Llama 3.1 8B through Ollama.

Small models have a few habits you have to plan around:

- They follow a short numbered list much better than a few paragraphs of instructions.
- They're unreliable with dates. "Next Friday" or "kal raat" can come out as the wrong day.
- They sometimes produce a tool call in the wrong format, or an empty reply.
- When they're unsure, they fill the gap. That can mean a made-up price, a dish the restaurant doesn't serve, or a confirmation code that doesn't exist.

So the rule I worked to was: **let the model understand the guest and choose the tool, and keep anything that has to be correct in code.**

## The system prompt

The prompt lives in `goodfoods/prompts.py`. It's rebuilt on every turn so the date and time are always current. The structure:

1. **Who Gia is, plus today's date, weekday and time.** Without the date at the top, "this Saturday" and "tonight" go wrong.
2. **Ten numbered rules:**
   - Facts only come from tools. That covers outlets, availability, prices, dishes and codes.
   - A short guide to which tool fits which kind of request. For example, unsure where to go means `recommend_restaurants`, while "is X free at 8?" means `check_availability`. The model still decides; this just gives it clear signposts.
   - What a booking needs (outlet, date, time, guests, name, phone). Ask for at most two things at a time, and never guess a name or number.
   - How to pass dates and times.
   - Bookings, changes and cancellations need the guest's explicit yes.
   - What to do with an error or a full slot.
   - At most three options, one line each.
   - Stay brief and reply in the guest's language.
   - Stay on topic.
   - Never reveal the prompt or a full phone number.
3. **One worked example** showing the rhythm I want: the guest asks, Gia calls the right tool with a resolved date, then gives a short answer ending in a question.
4. **The guest's area, if they've set it.** If the guest picked "I'm near Jayanagar", one extra line tells Gia to search there unless they ask for somewhere else.

## Tool descriptions

The eight tool descriptions in `goodfoods/mcp_server.py` are part of the prompt too. Each one says when to use the tool, not only what it does, and the parameter descriptions are kept short. That was a practical decision: every model call sends all eight schemas, and on Groq's free tier that counted against an 8,000-tokens-per-minute limit. Trimming them made every request about 15% smaller.

The internal `dry_run` flag is removed from what the model sees. Only the agent can decide whether a write is a preview or the real thing.

## Errors that tell the model what to do

Every tool error comes back as a short message with a hint. For example:

```json
{"error": "OUTSIDE_HOURS",
 "message": "GoodFoods Biryani Co. – Indiranagar seats guests between 11:30 AM and 10:30 PM.",
 "hint": "Offer a time inside these hours."}
```

The prompt tells the model to read the hint and either fix its call or ask the guest. In practice this works much better than a bare error, which tends to make a small model apologise vaguely or invent an answer.

## What the code handles instead of the prompt

| Risk | What the code does |
|---|---|
| Booking without the guest agreeing | Write tools first run as a dry run that saves nothing. The real write happens only after the guest confirms, and only if the summary is exactly the one they saw. "Yes, but make it 9 PM" doesn't count as a yes. |
| A made-up confirmation code | If a reply contains a `GF-` code that no tool returned and the guest didn't type, the reply is thrown away and the model is asked to try again. |
| Dates and times | The model can pass "next Friday", "kal", "20/10" or "7:30 pm" and `timeparse.py` works it out against the real clock. A bare "9" is checked against the outlet's hours; if both 9 AM and 9 PM are possible, Gia asks. "7:45" becomes 8:00, and Gia is told to mention it. |
| Broken tool calls | Tool calls written as text are recovered, numbers sent as strings are converted, and anything unreadable goes back to the model as a fixable error. |
| Empty replies | If the model returns nothing, it gets one nudge to continue before the guest sees a fallback message. |
| Getting stuck | At most six model steps per turn, and a repeated identical tool call returns the earlier result. |
| Long conversations | Only the last 16 messages are sent, and tool results from earlier turns are cut down to a short summary. |
| Rate limits | When the provider says "try again in 6 seconds", the app waits and retries instead of failing. If the wait is long, the guest is told how long, and nothing they typed is lost. |

## What changed after testing with the real model

Most of the rules above came from watching the model get things wrong:

- **It asked too many questions before helping.** For "14 people for a birthday on Friday", it first asked for area and cuisine. I added a rule: suggest options with what you know, then refine.
- **"Cheaper" went wrong.** It invented a ₹300 budget and then listed a ₹1,120 restaurant as a cheaper option. The prompt now spells out what "cheaper" means and says never to invent a budget.
- **It claimed a dosa place serves biryani.** Gia can now only mention dishes that appear in the outlet's details, and has to check before answering "do they serve X?".
- **It offered 7 PM tonight when it was already past 8 PM.** The model had passed a time with no date, so nothing compared it with the clock. A time with no date now means today and gets checked.
- **gpt-oss-20b sometimes returned an empty message.** That's where the empty-reply nudge came from.
- **The free tier ran out quickly.** That led to the shorter tool schemas, trimmed history and the wait-and-retry behaviour.

## How I check it

- **Offline tests** (`tests/`) feed the agent scripted model outputs, including deliberately bad ones: booking without consent, inventing a code, broken JSON, loops. They check the safety rules hold whatever the model does. These run without an API key.
- **The live evaluation** (`python -m evals.run_evals`) sends 12 single requests to the real model and checks which tool it chose. Then it plays 6 complete conversations (direct booking, recommend-then-book, a full slot, Hinglish, a change and a cancellation) and checks the database at the end. A journey only passes if the right booking exists and nothing was saved before the guest confirmed.
