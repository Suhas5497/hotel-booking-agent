# Design notes

These are the main decisions I made, why, and what I'd change with more time.

## Why build the agent from scratch

The brief rules out LangChain and similar frameworks, but I'd have kept it small anyway. The agent loop in `goodfoods/agent.py` is about 250 lines: send the conversation to the model, run any tool it asks for, send the result back, and repeat (at most six times per turn). Writing it myself meant I could put the safety checks exactly where they need to go, in between "the model wants to book" and "the booking is saved", instead of working around a framework's idea of how that should look.

## Why MCP

The tools are served by an MCP server (`goodfoods/mcp_server.py`) over JSON-RPC 2.0, and the agent talks to it through an MCP client. It would have been simpler to call Python functions directly, but going through MCP has two real benefits:

- **The same tools work outside this app.** Running `python -m goodfoods.mcp_server` makes them usable from any MCP client: Claude Desktop, an IDE, or a WhatsApp or phone bot later on.
- **The server declares which tools change data.** Each tool carries a read-only or write flag, and the agent reads those flags to decide what needs the guest's confirmation. Adding a new write tool later won't accidentally skip the confirmation step.

## Why the rules live in code

Table capacity, opening hours, 90-minute sittings, date parsing and identity checks are all plain Python in `services.py` and `timeparse.py`. A small model is good at understanding "Saturday night, 6 of us, near Indiranagar". It isn't reliable at working out whether the last 6-seat table is free at 8:30 when two other parties sit down at 7:30. Keeping those rules in code means they're testable and always give the same answer.

The most important one is the confirmation step. Booking, changing or cancelling runs as a dry run first and saves nothing. It's only saved after the guest confirms, and only if the details match the preview they saw.

## Why SQLite

It needs no setup, so a reviewer can run the project with one `pip install`. Bookings are written inside a transaction, so two people can't both get the last table. Moving to Postgres or to GoodFoods' real reservation system would only change the storage layer; the agent and the tools stay the same.

## How recommendations work

There are only 75 outlets with clear attributes (cuisine, area, price, rating, features), so I used a simple scoring formula instead of embeddings or a vector database. Each outlet gets points for:

| Factor | Weight |
|---|---|
| Matching the cuisine | 30% |
| Rating | 20% |
| Being in or near the guest's area | 15% |
| Fitting the budget | 15% |
| Having features that suit the occasion (for example rooftop or quiet for a date, private room for a big birthday) | 15% |
| Matching other words the guest used | 5% |

Then:

- Features the guest asks for by name, like "rooftop", become requirements rather than preferences.
- Outlets that are actually free at the requested time come first.
- The top three are spread across different restaurant styles.

Every suggestion comes with its reasons ("fits your ₹2,000 budget", "has rooftop, live music"), which is also what Gia uses to explain her picks. If GoodFoods later adds menus and reviews, semantic search would start to earn its place.

## Why the guided booking flow

Early testing showed that a pure chat interface makes guests do a lot of typing, and makes the model ask a lot of questions. So once a guest picks a restaurant, the chat shows buttons for day, guests and time, listing only the times that are actually free, plus small fields for name and phone. Every tap still goes to Gia as a normal message, so the model keeps making the decisions, but the guest spends less effort and the model gets cleaner input.

## The model

I started with Llama 3.1 8B on Groq. Groq retired it in August 2026, so the default is now `openai/gpt-oss-20b`, which is open-weight and in the same size and cost class. Because the model is reached through any OpenAI-compatible URL, switching was one line in `.env`, and the project still runs Llama 3.1 8B locally through Ollama. For a real deployment I'd prefer a self-hosted open-weight model, so guest data stays in India and the cost per conversation is predictable.

## Testing

- **99 offline tests** cover the booking rules, date parsing, the MCP protocol, and the agent's behaviour when the model misbehaves.
- **A UI walkthrough** clicks through every screen of the app with a scripted model.
- **The live evaluation** runs the real model through 12 single requests and 6 full conversations, then checks the database for the right outcome.

## What I'd do differently with more time

- Run the live evaluation on every change and track the results over time.
- Add a proper review of real conversations to find the next prompt fixes.
- Replace the generated outlet data with a real pilot outlet's tables and hours.
- Add OTP verification before anyone can change or cancel a booking.
