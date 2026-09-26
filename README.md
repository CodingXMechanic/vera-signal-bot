# Vera Signal Bot — deterministic merchant message engine

**Approach.** Most entries will be a single LLM prompt. This bot is the opposite: a
zero-LLM, fully deterministic composer. Every send arbitrates all verifiable facts
(trigger payload → merchant anomaly → category proof → live offer), picks **one hero
fact** to open with, then renders through a per-trigger-kind template in a
per-category voice (peer-clinical for dentists, fellow-operator for restaurants,
coach for gyms, warm-practical for salons, trustworthy-precise for pharmacies).
Nothing is ever invented — every number, name, date and price is quoted from the
pushed contexts, taboo words are scrubbed, and exactly one CTA is enforced
(`binary_yes_no` for actions, `open_ended` for digests/curiosity, `multi_choice_slot`
only for booking flows). Tick latency is ~1 ms, so the 30 s budget can never blow.

**Replies** run a small state machine per `conversation_id`: a canned-phrase +
repetition auto-reply ladder (flag once → wait 24 h → end), a multilingual
commit lexicon (en/hi/Hinglish: "haan bhej do", "let's do it", "send the abstract")
that flips pitch→action immediately, instant graceful `end` on hostility/opt-out,
polite decline + thread-back on off-topic asks (GST etc.), and a 3-nudge cap.
Suppression keys dedup re-sends; `send_as` follows trigger scope strictly, and a
customer-facing message is never composed without its `CustomerContext`.

**Tradeoffs.** Determinism and grounding beat eloquence: wording is assembled, not
authored, so phrasing varies less than an LLM's. I accept that — the judge
penalizes hallucination and generic copy hardest, and templates grounded in real
facts dominate both. Mid-test injections are handled by a generic renderer that
quotes verbatim payload facts rather than guessing.

**What would help most.** Per-merchant slot calendars and per-customer consent
timestamps (to sharpen recall offers), plus category-level "what worked last week"
proof points for stronger social-proof levers.

**Run.** `python bot.py --port 8080` (stdlib only; honors `$PORT` on hosts that
inject it). Self-test: `python test_local.py` (30/30 pass).
`submission.jsonl` covers all 25 seed triggers via `generate_submission.py`.

**Deploy (pick one, ~5 min).**
- Render: push this folder to GitHub → New → Blueprint → select repo
  (`render.yaml` sets Docker runtime + `/v1/healthz` check). Note the URL.
- Fly: `fly launch --no-deploy && fly deploy` (`fly.toml` included, port 8080).
- Railway/Heroku: connect repo, it uses `Procfile` (`web: python bot.py`).
- ngrok (quickest demo): `python bot.py` + `ngrok http 8080` → use the
  `https://…ngrok…` URL. Keep the laptop awake during evaluation.
Then submit that base URL in the portal — the judge calls
`GET /v1/healthz, GET /v1/metadata, POST /v1/context|tick|reply`.
Verify from any machine: `curl https://<your-url>/v1/healthz`.
