"""Vera reply engine — multi-turn conversation state machine.

Handles the three replay scenarios explicitly:
 1. auto-reply hell  -> detect canned phrasing + repetition, wait then end
 2. intent transition -> multilingual commit lexicon flips pitch -> action
 3. hostile/off-topic -> graceful exit or polite redirect

Also: language mirroring (hi <-> en), anti-repetition, 3-nudge cap.
State is kept per conversation_id in memory.
"""

import re

AUTO_PHRASES = [
    "thank you for contacting", "our team will respond shortly",
    "we will get back", "hamari team", "sampark karne ke liye dhanyavad",
    "auto-reply", "automated assistant", "i am an automated",
    "team tak pahuncha", "we have received your message",
    "please wait", "kindly wait", "business hours",
]

COMMIT_PATTERNS = [
    r"\bok(ay)?\b.*(do it|let'?s|karo|kar do|bhej|send|go ahead|proceed|confirm|done deal)",
    r"(let'?s do it|go ahead|what'?s next|proceed|confirm|bhej do|bhejo|kar do|shuru karo|haan kar)",
    r"^(yes|yeah|yep|haan|han|ha|ok|okay|theek hai|thik hai|bilkul|sure|done)\b.{0,40}(send|do|draft|post|schedule|book|renew|confirm|go|please|kar|bhej)",
    r"^(yes please|yes send|please send|draft (it|the)|schedule it|book (it|me)|renew (it|now))",
    r"(what would it look like|send (me )?the (list|abstract|draft|details)|share (the|details))",
]

HOSTILE = ["stop messaging", "stop sending", "useless", "spam", "bothering",
           "bakwas", "pareshan", "band karo", "fuck", "idiot", "stupid bot",
           "not interested", "don't (want|message|contact)", "unsubscribe",
           "remove me", "leave me alone", "do not contact"]

OFFTOPIC = ["gst", "tax filing", "ca ", "loan", "visa", "passport", "stock market",
            "crypto", "election", "horoscope", "matrimonial", "rent agreement"]

HI_MARKERS = ["haan", "nahi", "kya", "apke", "aapke", "ji ", "theek", "thik",
              "kar do", "bhej", "dhanyavad", "shukriya", "namaste", "accha"]


def _norm(s):
    if not isinstance(s, str):
        s = str(s or "")
    return re.sub(r"\s+", " ", s.lower()).strip()


def is_auto_reply(msg, history):
    m = _norm(msg)
    hits = sum(1 for p in AUTO_PHRASES if p in m)
    # repetition: same merchant text seen before (compare against prior turns)
    repeat = any(_norm(h) == m and h for h in history[-4:])
    formal_third = ("team" in m or "our " in m) and ("thank" in m or "inform" in m)
    if hits >= 1 and (repeat or formal_third or hits >= 2):
        return True
    # bare repetition of a long formal message is also canned-like
    if repeat and len(m) > 60 and ("thank" in m or "regard" in m or "team" in m):
        return True
    return False


def is_commit(msg):
    m = _norm(msg)
    return any(re.search(p, m) for p in COMMIT_PATTERNS)


def is_hostile(msg):
    m = _norm(msg)
    return any(h in m for h in HOSTILE)


def is_offtopic(msg):
    m = _norm(msg)
    return any(o in m for o in OFFTOPIC)


def reply(convo, merchant_msg, merchant=None, topic_hint=""):
    """convo: dict with keys turns(list), auto_streak(int), nudges(int), ended(bool).
    Returns dict action/send|wait|end + body/cta/rationale."""
    merchant = merchant or {}
    history = [t.get("msg", "") for t in convo.get("turns", []) if t.get("from") in ("merchant", "customer")]
    m = merchant_msg or ""

    if convo.get("ended"):
        return {"action": "end", "rationale": "conversation already closed; staying closed"}

    # 1. hostile / opt-out always wins
    if is_hostile(m):
        convo["ended"] = True
        return {"action": "end",
                "rationale": "explicit frustration/opt-out detected; closing without further sends"}

    # 2. auto-reply ladder: 1st -> one gentle flag, 2nd -> long wait, 3rd+ -> end
    if is_auto_reply(m, history):
        convo["auto_streak"] = convo.get("auto_streak", 0) + 1
        s = convo["auto_streak"]
        if s == 1:
            return {"action": "send",
                    "body": "Looks like an auto-reply — when the owner sees this, a quick 'Yes' is all I need to proceed.",
                    "cta": "binary_yes_no",
                    "rationale": "first canned auto-reply; one explicit owner-flag, staying open"}
        if s == 2:
            return {"action": "wait", "wait_seconds": 86400,
                    "rationale": "same auto-reply twice; owner not at phone; backing off 24h"}
        convo["ended"] = True
        return {"action": "end",
                "rationale": f"auto-reply {s}x in a row; zero engagement signal; closing"}

    convo["auto_streak"] = 0

    # 3. commit -> action mode immediately (never another qualifying question)
    if is_commit(m):
        owner = (merchant.get("identity", {}) or {}).get("owner_first_name", "there")
        hi = any(w in _norm(m) for w in HI_MARKERS)
        if hi:
            body = (f"Done samjho, {owner} — draft bana rahi hoon (90 sec). GBP post kal 10am ke liye pre-fill kar diya. "
                    f"Reply CONFIRM and I'll send it to the list.")
        else:
            body = (f"Great, {owner} — drafting now (90 seconds). I've also pre-filled the post for tomorrow 10am. "
                    f"Reply CONFIRM and I'll send it out.")
        return {"action": "send", "body": body, "cta": "binary_yes_no",
                "rationale": "explicit commit detected; switched pitch->action with concrete draft + CONFIRM gate"}

    # 4. off-topic -> polite decline + thread back
    if is_offtopic(m):
        back = f" Coming back to {topic_hint} — want me to proceed with the draft?" if topic_hint else " Want me to proceed with the draft I prepared?"
        return {"action": "send",
                "body": f"I'll have to leave that to your CA — outside what I can do well.{back}",
                "cta": "open_ended",
                "rationale": "out-of-scope ask declined politely; redirected to original trigger thread"}

    # 5. soft stall ("later", "busy") -> wait, capped
    if re.search(r"\b(later|busy|not now|baad me|kal baat|time nahi)\b", _norm(m)):
        return {"action": "wait", "wait_seconds": 10800,
                "rationale": "merchant asked for time; backing off 3h, one retry max"}

    # 6. default: acknowledge + advance one step, never repeat, never qualify twice
    convo["nudges"] = convo.get("nudges", 0) + 1
    if convo["nudges"] >= 3:
        convo["ended"] = True
        return {"action": "end", "rationale": "3 unanswered nudges; graceful exit to avoid spam"}
    owner = (merchant.get("identity", {}) or {}).get("owner_first_name", "there")
    topic = f" on {topic_hint}" if topic_hint else ""
    return {"action": "send",
            "body": f"Noted, {owner} — keeping this short{topic}: shall I send the ready draft + slot list? One-word reply works.",
            "cta": "binary_yes_no",
            "rationale": "open thread; low-friction binary close, no repeated question"}
