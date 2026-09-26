# Vera Signal Bot

My entry for the magicpin AI challenge — a message engine for Vera, the merchant assistant.

## What I built

Honestly, my first instinct was to wire up an LLM prompt and call it a day. But then
I re-read how the judging works: the judge scores grounded decisions, punishes
invented facts (-2 each), and throws fresh scenarios at the bot that it never saw
in training. A prompt that sounds clever on the 30 sample pairs can fall apart on
the hidden ones. So I went the boring-but-solid route: **no LLM at all**.

How it works, in plain words:

1. **Pick one hero fact.** For every send, the bot looks at the trigger first
   (what just happened?), then the merchant's own numbers (what's unusual for
   *them*?), then the category knowledge (what proof exists?). One fact wins and
   opens the message. Everything else stays out.
2. **Say it in the right voice.** A dentist gets a clinical, cited note ("JIDA Oct
   2026, p.14"). A restaurant owner gets operator talk ("covers", "delivery
   push"). A pharmacy customer gets a respectful Namaste message in Hindi.
3. **Never invent.** Every number, price, date and name is copied from the data
   the judge pushed. If the data doesn't have it, the message works around it
   instead of making it up. Bulk prices are computed off the merchant's real
   offer (₹149 → 149/135/125 slabs), never hardcoded.
4. **One ask, easy to answer.** Every message ends with a single low-effort step:
   reply YES, reply 1 or 2, or just say the word.

Where the LLM fits: it never decides facts. It has two gated jobs. (1) Rephrase
a finished draft for flow. (2) Draft messages for trigger kinds nobody has ever
seen — working only from the live contexts, with every number/name/CTA checked
against them. Any violation, or any API hiccup, falls back to the deterministic
draft untouched. Set `LLM_API_KEY` (plus optional `LLM_BASE_URL` / `LLM_MODEL`,
any OpenAI-compatible API) to enable it; without a key the bot runs fully
deterministic, and tick composes run in parallel so a slow provider can never
blow the 30s budget. The guarantee holds either way: this system cannot send
an ungrounded message.

Replies get the same treatment — a small state machine, not vibes: it spots
WhatsApp auto-replies (flags once, waits, then stops wasting everyone's time),
it hears "yes, do it" in English, Hindi or Hinglish and switches straight to
action, and it bows out gracefully when someone's annoyed.

## Files

- `bot.py` — the server (5 endpoints, plain Python, no packages to install)
- `composer.py` — the message writer (deterministic core)
- `polish.py` — the optional LLM rephraser + fact validator
- `reply_engine.py` — the conversation handler
- `submission.jsonl` — my 25 composed messages for the sample triggers
- `test_local.py` / `test_full.py` / `test_polish.py` — self-tests (31 + 234 + 11 checks, all passing)

## Run it

```bash
python bot.py --port 8080   # honors $PORT too, for hosts like Render
python test_full.py         # full verification
```

## Deploy it

Push to GitHub, then on Render: New → Blueprint → pick the repo (`render.yaml`
is included). Add a free UptimeRobot ping on `/v1/healthz` every 5 minutes so
the free tier never sleeps. Submit that URL.

## If I had more time

I'd add per-merchant send-time learning (when does *this* owner actually reply?)
and a second hero fact for high-urgency alerts. What would help most from
magicpin's side: real slot calendars and consent timestamps, so recall messages
can offer exact appointments instead of asking for a time.
