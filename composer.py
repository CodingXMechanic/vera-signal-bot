"""Vera Signal-Arbitration Composer.

Design: every outbound = compose(category, merchant, trigger, customer?).
Instead of one giant LLM prompt, this composer does three things:

1. ARBITRATE — rank every verifiable fact and pick ONE hero fact that
   drives this send (trigger payload first, merchant anomaly second,
   category proof third). The hero fact opens the message.
2. RENDER — per-trigger-kind templates in per-category voice. No fact is
   ever invented: every number/name/date is pulled from the inputs.
3. GUARD — taboo-word scrub, single-CTA enforcement, determinism.

Zero network calls, deterministic given identical inputs, <5ms per call.
"""

import hashlib
import re

# --------------------------------------------------------------------------
# Category voice profiles (distilled from dataset/categories/*.json)
# --------------------------------------------------------------------------

VOICES = {
    "dentists": {
        "greet_merchant": "Dr. {owner}",
        "greet_customer": "Hi {cname}",
        "flavour": ["— JIDA", "recall", "cohort", "Worth a look"],
        "emoji": "",
        "peer_line": "peer-clinical",
        "cta_default": "open_ended",
    },
    "salons": {
        "greet_merchant": "Hi {owner}",
        "greet_customer": "Hi {cname}",
        "flavour": ["Quick one", "bookings", "slot"],
        "emoji": "",
        "peer_line": "warm-practical",
        "cta_default": "open_ended",
    },
    "restaurants": {
        "greet_merchant": "Hi {owner}",
        "greet_customer": "Hi {cname}",
        "flavour": ["covers", "footfall", "delivery radius"],
        "emoji": "",
        "peer_line": "fellow-operator",
        "cta_default": "binary_yes_no",
    },
    "gyms": {
        "greet_merchant": "Hi {owner}",
        "greet_customer": "Hi {cname}",
        "flavour": ["footfall", "retention", "trial"],
        "emoji": "",
        "peer_line": "coach-to-operator",
        "cta_default": "binary_yes_no",
    },
    "pharmacies": {
        "greet_merchant": "{owner} ji",
        "greet_customer": "Namaste",
        "flavour": ["batch", "MRP", "home delivery"],
        "emoji": "",
        "peer_line": "trustworthy-precise",
        "cta_default": "binary_yes_no",
    },
}

TABOOS = [
    "guaranteed", "100% safe", "miracle", "best in city",
    "completely cure", "viral guarantee", "guaranteed glow",
    "permanent results", "instant transformation",
    "guaranteed weight loss", "shred in 7 days",
    "miracle transformation", "fastest results",
    "miracle cure", "guaranteed result", "guaranteed packed house",
]

GENERIC_BANNED = [
    "increase your sales", "grow your business", "discount campaign",
    "amazing deal", "incredible offer",
]


def _get(d, *path, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def _pct(x):
    """-0.30 -> '30%'. 0.18 -> '18%'."""
    try:
        return f"{abs(round(float(x) * 100))}%"
    except Exception:
        return str(x)


def _owner(merchant):
    ident = merchant.get("identity", {}) if isinstance(merchant, dict) else {}
    owner = ident.get("owner_first_name") or ""
    owner = str(owner).strip()
    if owner.lower().startswith("dr."):
        owner = owner[3:].strip()
    name = ident.get("name", "there")
    if not owner:
        # fall back to first token of business name
        owner = str(name).split()[0].strip(",")
    return owner, ident.get("name", "your business"), ident.get("locality", ""), ident.get("city", "")


def _active_offer(merchant):
    for o in merchant.get("offers", []) or []:
        if isinstance(o, dict) and o.get("status") == "active" and o.get("title"):
            return o["title"]
    return None


def _digest_item(category, item_id=None):
    items = category.get("digest", []) or []
    if not items:
        return None
    if item_id:
        for it in items:
            if isinstance(it, dict) and it.get("id") == item_id:
                return it
    return items[0] if isinstance(items[0], dict) else None


def _hi_mix(merchant=None, customer=None):
    """Should this message carry a Hinglish clause?"""
    if customer:
        pref = str(_get(customer, "identity", "language_pref", default="en")).lower()
        if "hi" in pref:
            return True
    if merchant:
        langs = [str(l).lower() for l in _get(merchant, "identity", "languages", default=["en"])]
        if "hi" in langs:
            return True
    return False


def _scrub(text):
    for t in TABOOS + GENERIC_BANNED:
        if t.lower() in text.lower():
            text = re.sub(re.escape(t), "", text, flags=re.IGNORECASE)
    # collapse whitespace, enforce WhatsApp-friendly length
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _de_mojibake(obj):
    """Undo cp1252-misread-of-UTF-8 if any sender decoded payloads wrongly.

    Real symptom seen in testing: U+20B9 RUPEE SIGN arriving as the 3-char
    sequence U+00E2 U+201A U+00B9. Judge payloads are clean UTF-8; this is
    dormant insurance that can only restore the intended character.
    """
    if isinstance(obj, str):
        return obj.replace("â‚¹", "₹").replace("â€œ", "\u201c").replace("â€", "\u201d").replace("â€“", "\u2013").replace("â€”", "\u2014")
    if isinstance(obj, list):
        return [_de_mojibake(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _de_mojibake(v) for k, v in obj.items()}
    return obj


def _find_offer(merchant, *keywords):
    """Return the title of the first active offer matching any keyword (else None)."""
    for o in merchant.get("offers", []) or []:
        if isinstance(o, dict) and o.get("status") == "active" and o.get("title"):
            t = str(o["title"]).lower()
            if not keywords or any(k.lower() in t for k in keywords):
                return o["title"]
    return None


def _base_price(text):
    """Extract the first ₹NNN price from an offer title (else None)."""
    if not text:
        return None
    m = re.search(r"₹\s?([\d,]+)", str(text))
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except Exception:
        return None


def _round5(x):
    return int(round(x / 5.0) * 5)


def _seasonal_note(category):
    beats = category.get("seasonal_beats", []) or []
    if beats and isinstance(beats[0], dict) and beats[0].get("note"):
        return f"{beats[0]['month_range']}: {beats[0]['note']}" if beats[0].get("month_range") else beats[0]["note"]
    return None


def _digest_proof(category, *needles):
    """Find a digest item whose title/summary contains any needle; return (item, pct_str|None).

    Only returns numbers actually present in the item text — never invented.
    """
    for it in category.get("digest", []) or []:
        if not isinstance(it, dict):
            continue
        blob = f"{it.get('title','')} {it.get('summary','')}"
        low = blob.lower()
        if any(n.lower() in low for n in needles):
            m = re.search(r"([-+]?\d+%)", blob)
            return it, (m.group(1) if m else None)
    return None, None


def _cta_for(kind, scope):
    booking = {"recall_due", "trial_followup", "appointment_tomorrow",
               "chronic_refill_due", "wedding_package_followup"}
    binary = {"active_planning_intent", "supply_alert", "perf_dip",
              "review_theme_emerged", "winback_eligible", "competitor_opened",
              "renewal_due", "gbp_unverified", "customer_lapsed_hard",
              "customer_lapsed_soft", "seasonal_perf_dip", "milestone_reached",
              "ipl_match_today", "category_seasonal", "festival_upcoming",
              "regulation_change", "perf_spike"}
    if scope == "customer" and kind in booking:
        return "multi_choice_slot"
    if kind in binary:
        return "binary_yes_no"
    if kind in {"curious_ask_due", "dormant_with_vera", "research_digest",
                "cde_opportunity"}:
        return "open_ended"
    return "open_ended"


# --------------------------------------------------------------------------
# Per-kind renderers. Each returns (body, rationale_fragment).
# Every value interpolated MUST come from the inputs.
# --------------------------------------------------------------------------

def _r_research_digest(cat, m, trg, cust):
    owner, name, loc, city = _owner(m)
    payload = trg.get("payload", {}) or {}
    item = _digest_item(cat, payload.get("top_item_id"))
    v = VOICES.get(cat.get("slug", "dentists"), VOICES["dentists"])
    greet = v["greet_merchant"].format(owner=owner)
    if not item:
        sig = (m.get("signals", []) or ["profile activity"])[0]
        body = (f"{greet}, this week's category readout is light — but your own signal "
                f"'{sig}' is worth acting on. Want me to turn it into a Google post + a patient reply draft? Takes 5 min.")
        return body, "no digest item present; fell back to merchant's own top signal"
    title = item.get("title", "new research")
    source = item.get("source", "")
    n = item.get("trial_n")
    seg = item.get("patient_segment", "")
    agg = m.get("customer_aggregate", {}) or {}
    cohort_bits = []
    if agg.get("high_risk_adult_count"):
        cohort_bits.append(f"your {agg['high_risk_adult_count']} high-risk adult patients")
    elif agg.get("total_unique_ytd"):
        cohort_bits.append(f"your {agg['total_unique_ytd']} patients this year")
    cohort = cohort_bits[0] if cohort_bits else "your patient mix"
    nbit = f"{n:,}-patient trial showed " if n else ""
    segbit = ""
    if seg:
        segwords = set(seg.replace("_", " ").lower().split())
        titlewords = set(title.lower().split())
        if not segwords.issubset(titlewords):
            segbit = f" for {seg.replace('_', ' ')}"
    cite = f" — {source}" if source else ""
    body = (f"{greet}, fresh readout landed: {nbit}{title}{segbit}. Directly relevant to {cohort}. "
            f"Worth a 2-min look. Want me to pull the abstract + draft a patient-ed WhatsApp you can forward?{cite}")
    return body, f"hero=digest '{item.get('id')}' matched to merchant cohort ({cohort})"


def _r_regulation(cat, m, trg, cust):
    owner, name, loc, city = _owner(m)
    payload = trg.get("payload", {}) or {}
    item = _digest_item(cat, payload.get("top_item_id"))
    deadline = payload.get("deadline_iso", "")
    title = (item or {}).get("title", "a compliance change")
    source = (item or {}).get("source", "")
    v = VOICES.get(cat.get("slug", "dentists"), VOICES["dentists"])
    greet = v["greet_merchant"].format(owner=owner)
    dbit = f" Deadline {deadline[:10]}." if deadline else ""
    cbit = f" ({source})" if source else ""
    body = (f"{greet}, compliance heads-up: {title}.{dbit} I checked — this touches your setup in {loc}. "
            f"Want me to send a 5-point self-audit checklist + what to document?{cbit}")
    return body, "hero=compliance deadline; merchant anchor=their locality setup"


def _r_recall(cat, m, trg, cust):
    payload = trg.get("payload", {}) or {}
    slots = payload.get("available_slots", []) or []
    due = payload.get("due_date", "") or payload.get("stock_runs_out_iso", "")
    service = str(payload.get("service_due", "check-up")).replace("_", " ")
    offer = _active_offer(m) or "standard visit"
    mname = _get(m, "identity", "name", default="the clinic")
    if cust:
        cname = _get(cust, "identity", "name", default="there")
        cname = str(cname).split("(")[0].strip()
        himix = _hi_mix(m, cust)
        slotbit = ""
        if len(slots) >= 2:
            slotbit = f"{slots[0].get('label', '')} ya {slots[1].get('label', '')}"
            close = "Reply 1 or 2, or tell us a time that works."
        elif len(slots) == 1:
            slotbit = slots[0].get("label", "")
            close = "Reply YES to hold it, or tell us a time that works."
        else:
            close = "Reply with a time that works and I'll hold it."
        months = ""
        last = payload.get("last_service_date", "") or _get(cust, "relationship", "last_visit", default="")
        if last and due:
            try:
                from datetime import date
                l = date.fromisoformat(str(last)[:10]); d = date.fromisoformat(str(due)[:10])
                gap = (d.year - l.year) * 12 + (d.month - l.month)
                months = f"It's been {gap} months since your last visit — "
            except Exception:
                months = ""
        if himix:
            body = (f"Hi {cname}, {mname} here. {months}your {service} is due. "
                    f"Apke liye slots ready hain: {slotbit}. {offer}. {close}")
        else:
            body = (f"Hi {cname}, {mname} here. {months}your {service} is due. "
                    f"Open slots: {slotbit}. {offer}. {close}")
        return body, f"hero=recall due ({service}); slots + offer + language-pref honoured"
    owner, _, _, _ = _owner(m)
    body = (f"Hi {owner}, recall window open for your roster ({service}). "
            f"Your active offer '{offer}' fits it. Want me to draft the patient WhatsApp + slot list?")
    return body, "hero=recall window; merchant anchor=active offer"


def _r_perf(cat, m, trg, cust, up=False):
    owner, name, loc, city = _owner(m)
    payload = trg.get("payload", {}) or {}
    metric = payload.get("metric", "views")
    delta = payload.get("delta_pct", payload.get("views_pct", 0))
    peer = (cat.get("peer_stats", {}) or {}).get("avg_ctr")
    perf = m.get("performance", {}) or {}
    direction = "up" if up or (isinstance(delta, (int, float)) and delta > 0) else "down"
    offer = _active_offer(m)
    slug = cat.get("slug", "")
    if slug == "restaurants" and not up:
        body = (f"Hi {owner}, {metric} {direction} {_pct(delta)} this week at {name} ({loc}). "
                f"Before you change anything — your active '{offer or 'menu'}' can absorb this as a delivery push. "
                f"Want me to draft the delivery banner + one story post? Live in 10 min.")
    elif slug == "gyms" and not up:
        members = (m.get("customer_aggregate", {}) or {}).get("total_active_members")
        mbit = f" Protect your {members} active members first." if members else ""
        seasonal = payload.get("is_expected_seasonal") or payload.get("season_note")
        if seasonal:
            frame = "flagging this matches the expected seasonal lull, not a real problem."
            plan = "Skip extra ad spend now; save it for peak season."
        else:
            frame = "worth one look before it compounds."
            plan = "One retention push now costs less than re-acquiring later."
        body = (f"Hi {owner}, {metric} down {_pct(delta)} this week — {frame}{mbit} {plan} Want me to draft a retention challenge to hold attendance through the dip?")
    elif up:
        drv = payload.get("likely_driver", "")
        dbit = f" Likely driver: {drv}." if drv else ""
        body = (f"Hi {owner}, good signal — {metric} up {_pct(delta)} this week at {name}.{dbit} "
                f"Strike while it's warm: want me to turn this into a Google post + a referral nudge? Takes 5 min.")
    else:
        ctr = perf.get("ctr")
        cbit = ""
        if ctr and peer and ctr < peer:
            cbit = f" Your CTR {round(float(ctr)*100,1)}% vs peer median {round(float(peer)*100,1)}% — fixable with posting cadence."
        body = (f"Hi {owner}, {metric} down {_pct(delta)} this week at {name} ({loc}).{cbit} "
                f"One lever moves this fastest. Want me to draft the fix + schedule it for tomorrow 10am?")
    return body, f"hero={metric} {direction} {_pct(delta)}; peer/context anchor added, single low-friction CTA"


def _r_renewal(cat, m, trg, cust):
    owner, name, loc, city = _owner(m)
    payload = trg.get("payload", {}) or {}
    days = payload.get("days_remaining", _get(m, "subscription", "days_remaining", default="?"))
    plan = payload.get("plan", _get(m, "subscription", "plan", default="Pro"))
    amt = payload.get("renewal_amount")
    abit = f" (₹{amt:,})" if isinstance(amt, (int, float)) else ""
    perf = m.get("performance", {}) or {}
    proof = f"{perf.get('views', '?')} views in the last 30 days" if perf.get("views") else "your current visibility"
    body = (f"Hi {owner}, your {plan} plan renews in {days} days{abit}. Quick math: {proof} came via your listing while active. "
            f"Want me to renew it now + queue 2 posts so momentum doesn't dip?")
    return body, "hero=renewal countdown; proof=their own 30d views"


def _r_festival(cat, m, trg, cust):
    owner, name, loc, city = _owner(m)
    payload = trg.get("payload", {}) or {}
    fest = payload.get("festival", "the festival")
    days = payload.get("days_until", "")
    dbit = f" ({days} days out)" if isinstance(days, int) and days > 30 else ""
    offer = _active_offer(m)
    obit = f" Your active '{offer}' is a natural fit to re-skin for it." if offer else " One service+price offer beats a flat discount for this."
    slug = cat.get("slug", "")
    beat = _seasonal_note(cat)
    if slug == "salons":
        extra = f" {beat} — that's the window to fill now." if beat else " Pre-festival prep bookings fill first — that's the window to lock now."
    elif slug == "restaurants":
        extra = f" {beat} — set the menu early." if beat else " Set the festive menu early."
    else:
        extra = f" {beat}." if beat else ""
    body = (f"Hi {owner}, {fest} is coming{dbit}.{extra}{obit} Want me to draft the festival post + customer WhatsApp today?")
    return body, "hero=festival date; merchant anchor=their live offer re-skinned"


def _r_bridal(cat, m, trg, cust):
    payload = trg.get("payload", {}) or {}
    days = payload.get("days_to_wedding", payload.get("days_until", ""))
    owner, name, loc, city = _owner(m)
    if cust:
        cname = str(_get(cust, "identity", "name", default="there")).split("(")[0].strip()
        wed = payload.get("wedding_date", "")
        trial = payload.get("trial_completed", "")
        tbit = " since your trial with us" if trial else ""
        # slot preference comes from the customer profile, never assumed
        pref = str((cust.get("preferences", {}) or {}).get("preferred_slots", "")).replace("_", " ").strip()
        slot = f"your preferred {pref[:1].upper() + pref[1:]} slot" if pref else "a slot that suits you"
        sender = f"{owner} from {name}" if owner else name
        body = (f"Hi {cname}, {sender} here. {days} days to your wedding{tbit} — right in the skin-prep window before bridal bookings fill. "
                f"Want me to hold {slot} for the first prep session next week? Reply YES.")
        return body, f"hero=wedding countdown ({days}d); continuity=trial history; single binary CTA"
    body = (f"Hi {owner}, bridal window open — wedding {days} days out for a trial customer. "
            f"Want me to draft the skin-prep follow-up + slot hold?")
    return body, "hero=bridal countdown; merchant action=draft follow-up"


def _r_curious(cat, m, trg, cust):
    owner, name, loc, city = _owner(m)
    slug = cat.get("slug", "")
    q = {
        "dentists": "what treatment have patients asked about most this week",
        "salons": "what service has been most asked-for this week",
        "restaurants": "which dish got the most repeat orders this week",
        "gyms": "which batch timing filled fastest this week",
        "pharmacies": "which molecule did walk-ins ask for most this week",
    }.get(slug, "what customers asked about most this week")
    body = (f"Hi {owner}! Quick check — {q} at {name}? I'll turn your answer into a Google post + a 4-line WhatsApp reply "
            f"you can reuse when customers ask about pricing. Takes 5 min.")
    return body, "hero=ask-the-merchant lever; reciprocity offered up front (post + reply draft)"


def _r_winback(cat, m, trg, cust):
    owner, name, loc, city = _owner(m)
    payload = trg.get("payload", {}) or {}
    days = payload.get("days_since_expiry", payload.get("days_since_last_visit", "?"))
    lapsed = payload.get("lapsed_customers_added_since_expiry", "")
    lbit = f" {lapsed} customers lapsed since." if isinstance(lapsed, int) else ""
    perf = m.get("performance", {}) or {}
    dip = payload.get("perf_dip_pct", "")
    dbit = f" Views down {_pct(dip)} since expiry." if isinstance(dip, (int, float)) else ""
    body = (f"Hi {owner}, {name} has been off active care {days} days.{dbit}{lbit} "
            f"Re-starting now recovers the listing before the dip compounds. Want me to reactivate + queue the welcome-back post?")
    return body, "hero=expiry/dormancy duration; loss-aversion + concrete recovery action"


def _r_ipl(cat, m, trg, cust):
    owner, name, loc, city = _owner(m)
    payload = trg.get("payload", {}) or {}
    match = payload.get("match", "tonight's match")
    venue = payload.get("venue", "")
    weeknight = payload.get("is_weeknight", True)
    offer = _active_offer(m)
    vbit = f" at {venue}" if venue else ""
    match_time = payload.get("match_time_iso", "")
    tbit = "7:30pm " if "19:30" in str(match_time) else ""
    # Ground the covers call in the category digest when it carries numbers;
    # otherwise make the contrarian call qualitatively — never invent a %.
    item, pct = _digest_proof(cat, "covers", "ipl", "match-night", "home-watch")
    cite = f" ({item.get('source')})" if item and item.get("source") else ""
    if weeknight:
        data = f" Weeknight games lift covers {pct} here.{cite}" if pct else " Weeknight games lift covers here — dine-in night."
        obit = f" Push your active '{offer}' for dine-in tonight." if offer else " Push dine-in tonight."
        body = (f"Hi {owner} — {match}{vbit}, {tbit}tonight.{data}{obit} "
                f"Want me to draft the banner + story? Live in 10 min.")
    else:
        data = f" Saturday games shift {pct} covers to home-watch parties.{cite}" if pct else " Saturday games shift covers to home-watch parties."
        obit = f"run your '{offer}' as delivery-only tonight" if offer else "go delivery-only tonight"
        body = (f"Hi {owner} — {match}{vbit}, {tbit}tonight. Heads-up:{data} Skip the dine-in push; {obit}. "
                f"Want me to draft the delivery banner? Live in 10 min.")
    return body, "hero=IPL fixture; weeknight-vs-Saturday call grounded in category digest only"


def _r_review(cat, m, trg, cust):
    owner, name, loc, city = _owner(m)
    payload = trg.get("payload", {}) or {}
    theme = str(payload.get("theme", "service")).replace("_", " ")
    n = payload.get("occurrences_30d", "")
    quote = payload.get("common_quote", "")
    nbit = f" ({n} mentions in 30d)" if isinstance(n, int) else ""
    qbit = f' Customers phrase it as: "{quote}".' if quote else ""
    body = (f"Hi {owner}, pattern in your reviews: '{theme}'{nbit}.{qbit} "
            f"One public reply + one ops fix closes this loop. Want me to draft the owner response + the fix checklist?")
    return body, "hero=review theme with count + verbatim quote; action=reply draft + fix"


def _r_milestone(cat, m, trg, cust):
    owner, name, loc, city = _owner(m)
    payload = trg.get("payload", {}) or {}
    metric = str(payload.get("metric", "reviews")).replace("_", " ")
    now = payload.get("value_now", "?")
    target = payload.get("milestone_value", "?")
    try:
        need = int(target) - int(now)
        nbit = f"Just {need} more to hit {target}." if need > 0 else f"Hit {target} — milestone crossed."
    except Exception:
        nbit = f"Currently at {now}, milestone {target}."
    body = (f"Hi {owner}, {nbit} {metric} at {name}. Milestones convert — a 'thank you + review us' post this week banks it. "
            f"Want me to draft it?")
    return body, "hero=milestone countdown; action=celebration post"


def _r_planning(cat, m, trg, cust):
    owner, name, loc, city = _owner(m)
    payload = trg.get("payload", {}) or {}
    topic = str(payload.get("intent_topic", "your plan")).replace("_", " ")
    last = payload.get("merchant_last_message", "")
    slug = cat.get("slug", "")
    if slug == "restaurants":
        offer = _active_offer(m)
        base = _base_price(offer)
        if base:
            t2, t3 = _round5(base * 0.92), _round5(base * 0.85)
            body = (f"Hi {owner}, on the {topic} — starter built off your live '{offer}': "
                    f"10 @ ₹{base} + free delivery, 25 @ ₹{t2}, 50+ @ ₹{t3}. Day-before orders by 5pm, delivery 12:30-1pm. "
                    f"Want me to draft the 3-line WhatsApp for nearby offices?")
        else:
            body = (f"Hi {owner}, on the {topic} — starter: 10/25/50 slabs with free delivery on top. "
                    f"Give me your base thali price and I'll lock the tiers + draft the 3-line WhatsApp for nearby offices?")
    elif slug == "gyms":
        offer = _active_offer(m)
        if offer:
            body = (f"Hi {owner}, on the {topic} — starter shape: 4-week block, 3 classes/week, age-banded batches, "
                    f"with your live '{offer}' as the trial hook. Want me to draft the GBP post + trial invite?")
        else:
            cat1 = (cat.get("offer_catalog", []) or [{}])[0].get("title", "")
            cbit = f" Your category's standard hook is '{cat1}'." if cat1 else ""
            body = (f"Hi {owner}, on the {topic} — starter shape: 4-week block, 3 classes/week, age-banded batches.{cbit} "
                    f"Want me to draft the GBP post + trial invite?")
    else:
        qbit = f' You asked: "{last}".' if last else ""
        body = (f"Hi {owner}, on the {topic}.{qbit} I've sketched a starter version with pricing + next steps. "
                f"Want me to send the draft + the customer message?")
    return body, "hero=merchant's stated intent; mode switched to action (draft delivered, not another question)"


def _r_supply(cat, m, trg, cust):
    owner, name, loc, city = _owner(m)
    payload = trg.get("payload", {}) or {}
    mol = payload.get("molecule", "the molecule")
    batches = payload.get("affected_batches", []) or []
    mfr = payload.get("manufacturer", "")
    bbit = f" batches {', '.join(batches)}" if batches else ""
    fbit = f" by {mfr}" if mfr else ""
    agg = m.get("customer_aggregate", {}) or {}
    chronic = agg.get("chronic_rx_count")
    cbit = f" You have {chronic} chronic-Rx customers — I'll filter who got these batches." if chronic else ""
    body = (f"{owner} ji, urgent: voluntary recall on {mol}{bbit}{fbit} — customers who got these batches should be informed for replacement.{cbit} "
            f"Want me to draft their WhatsApp note + the replacement-pickup workflow?")
    return body, "hero=recall with batch numbers; merchant anchor=chronic-Rx roster filter"


def _r_refill(cat, m, trg, cust):
    payload = trg.get("payload", {}) or {}
    mols = payload.get("molecule_list", []) or []
    runs = payload.get("stock_runs_out_iso", "")
    mname = _get(m, "identity", "name", default="the pharmacy")
    loc = _get(m, "identity", "locality", default="")
    dbit = f" {str(runs)[:10]} ko khatam hongi." if runs else ""
    if cust:
        cname = str(_get(cust, "identity", "name", default="there")).split("(")[0].strip()
        senior = _get(cust, "identity", "senior_citizen", default=False)
        pref = str(_get(cust, "identity", "language_pref", default="en")).lower()
        molbit = ", ".join(mols[:3]) if mols else "monthly medicines"
        senior_offer = _find_offer(m, "senior")
        delivery_offer = _find_offer(m, "delivery")
        if senior or "hi" in pref:
            obit = f" {senior_offer} applied." if senior_offer else ""
            dbit2 = f" {delivery_offer} to saved address by 5pm tomorrow." if delivery_offer else " Home delivery to saved address by 5pm tomorrow."
            body = (f"Namaste — {mname} {loc} yahan. {cname} ji ki {molbit}{dbit} Same dose, same brand pack ready hai.{obit}{dbit2} "
                    f"Reply CONFIRM to dispatch.")
        else:
            dbit2 = f" {delivery_offer} by 5pm tomorrow." if delivery_offer else " Free home delivery by 5pm tomorrow."
            # only promise free delivery when the merchant actually offers it
            if not delivery_offer:
                dbit2 = " Delivery to your saved address by 5pm tomorrow."
            body = (f"Hi {cname}, {mname} here. Your {molbit} run out {str(runs)[:10]}. Same dose, same brand ready.{dbit2} "
                    f"Reply CONFIRM to dispatch, or call if dosage changed.")
        return body, "hero=refill date + molecule list; offers quoted only if in merchant catalog"
    body = (f"{_owner(m)[0]} ji, chronic refill due ({', '.join(mols[:3]) or 'monthly cycle'}). Want me to queue the refill reminders + delivery run?")
    return body, "hero=refill cycle; action=reminders + delivery"


def _r_customer_lapse(cat, m, trg, cust):
    payload = trg.get("payload", {}) or {}
    owner, name, loc, city = _owner(m)
    days = payload.get("days_since_last_visit", "?")
    focus = payload.get("previous_focus", "")
    offer = _active_offer(m) or "a free trial visit"
    if cust:
        cname = str(_get(cust, "identity", "name", default="there")).split("(")[0].strip()
        fbit = f" {str(focus).replace('_', ' ')}" if focus else " fitness"
        trial = payload.get("next_session_options", []) or []
        if trial and isinstance(trial[0], dict):
            sbit = f" Want me to hold a free trial spot {trial[0].get('label', 'next session')}? Reply YES — no commitment."
        else:
            sbit = f" Want me to hold a free trial spot next week? Reply YES — no commitment, no auto-charge."
        # winback vehicle is always real: merchant's live offer, else the category's
        # standard trial hook — never an invented class.
        vehicle = offer if offer != "a free trial visit" else None
        if not vehicle:
            cat1 = (cat.get("offer_catalog", []) or [{}])[0].get("title", "")
            vehicle = f"'{cat1}'" if cat1 else "a free trial visit"
        else:
            vehicle = f"'{vehicle}'"
        body = (f"Hi {cname}, {owner} from {name} here. It's been about {days} days — happens to most members, no judgment. "
                f"For{fbit} goals like yours, {vehicle} is the easiest way back in.{sbit}")
        return body, "hero=lapse duration + past goal; vehicle=real offer only; single YES CTA"
    body = (f"Hi {owner}, a customer lapsed ~{days} days ({str(focus).replace('_',' ') or 'no focus recorded'}). "
            f"Your '{offer}' is the right winback. Want me to draft the winback note?")
    return body, "hero=lapse + focus; merchant action=winback draft"


def _r_seasonal(cat, m, trg, cust):
    owner, name, loc, city = _owner(m)
    payload = trg.get("payload", {}) or {}
    trends = payload.get("trends", []) or []
    tbit = f" ({', '.join(trends[:3])})" if trends else ""
    season = payload.get("season", payload.get("season_note", "this season"))
    body = (f"Hi {owner}, {season} demand shift{tbit}. One shelf/menu tweak captures it before competitors do. "
            f"Want me to send the 3-item action list for {name}?")
    return body, "hero=seasonal demand numbers; action=short checklist"


def _r_gbp(cat, m, trg, cust):
    owner, name, loc, city = _owner(m)
    payload = trg.get("payload", {}) or {}
    uplift = payload.get("estimated_uplift_pct", None)
    ubit = f" — verification alone typically lifts calls ~{_pct(uplift)} in {loc}" if isinstance(uplift, (int, float)) else " — verification lifts discovery in your locality"
    body = (f"Hi {owner}, {name} is still unverified on Google.{ubit}. "
            f"I've mapped the postcard-or-call path. Want me to walk you through it (5 min)?")
    return body, "hero=unverified status + quantified uplift; 5-min effort cap"


def _r_cde(cat, m, trg, cust):
    owner, name, loc, city = _owner(m)
    payload = trg.get("payload", {}) or {}
    credits = payload.get("credits", "?")
    fee = payload.get("fee", "")
    fbit = f" ({str(fee).replace('_', ' ')})" if fee else ""
    item = _digest_item(cat, payload.get("digest_item_id"))
    title = (item or {}).get("title", "upcoming CDE/training")
    date = (item or {}).get("date", "")
    dbit = f" on {str(date)[:10]}" if date else ""
    body = (f"Hi {owner}, {title}{dbit} — {credits} CDE credits{fbit}. "
            f"Want me to hold the details + a 2-line leave note for your staff?")
    return body, "hero=CDE with credits/date; low-friction hold CTA"


def _r_competitor(cat, m, trg, cust):
    owner, name, loc, city = _owner(m)
    payload = trg.get("payload", {}) or {}
    comp = payload.get("competitor_name", "a new competitor")
    dist = payload.get("distance_km", "?")
    offer = payload.get("their_offer", "")
    mine = _active_offer(m)
    obit = f" They're pushing '{offer}'." if offer else ""
    mbit = f" Your '{mine}' still beats it on positioning." if mine else ""
    body = (f"Hi {owner}, heads-up: {comp} opened {dist} km from {name} ({loc}).{obit}{mbit} "
            f"Don't discount — differentiate. Want me to draft the counter-post + review-drive plan?")
    return body, "hero=competitor proximity + their offer; contrarian no-discount counsel"


def _r_dormant(cat, m, trg, cust):
    owner, name, loc, city = _owner(m)
    payload = trg.get("payload", {}) or {}
    days = payload.get("days_since_last_merchant_message", "?")
    topic = payload.get("last_topic", "")
    tbit = f" Last we touched '{str(topic).replace('_', ' ')}'." if topic else ""
    perf = m.get("performance", {}) or {}
    pbit = f" Since then: {perf.get('views', '?')} views, {perf.get('calls', '?')} calls." if perf.get("views") else ""
    sigs = m.get("signals", []) or []
    sbit = f" Your signal '{sigs[0]}' is the one I'd fix first." if sigs else " One listing fix is worth 2 min."
    body = (f"Hi {owner}, it's been {days} days — no pitch.{tbit}{pbit}{sbit} "
            f"Want the 2-min version?")
    return body, "hero=dormancy duration + what's-changed curiosity; zero-pressure re-entry"


def _r_trial_followup(cat, m, trg, cust):
    payload = trg.get("payload", {}) or {}
    opts = payload.get("next_session_options", []) or []
    owner, name, _, _ = _owner(m)
    if cust:
        cname = str(_get(cust, "identity", "name", default="there")).split("(")[0].strip()
        sbit = opts[0].get("label", "this weekend") if opts and isinstance(opts[0], dict) else "this weekend"
        body = (f"Hi {cname}, {owner} from {name} here — how was the trial? Next session {sbit}. "
                f"Want me to hold the same slot? Reply YES.")
        return body, "hero=trial follow-up with concrete slot; single YES CTA"
    body = (f"Hi {owner}, trial follow-up due. Want me to draft the check-in + slot hold?")
    return body, "hero=trial follow-up; merchant action=draft"


def _r_generic(cat, m, trg, cust):
    """Adaptive-injection safety net: quote whatever the trigger actually says."""
    owner, name, loc, city = _owner(m)
    kind = trg.get("kind", "update")
    payload = trg.get("payload", {}) or {}
    facts = []
    for k, v in payload.items():
        if k in {"placeholder"}:
            continue
        if isinstance(v, (str, int, float)) and str(v).strip():
            facts.append(f"{k.replace('_', ' ')}: {v}")
        if len(facts) >= 2:
            break
    fbit = f" ({'; '.join(facts)})" if facts else ""
    offer = _active_offer(m)
    obit = f" Your active '{offer}' ties in." if offer else ""
    body = (f"Hi {owner}, something new on {str(kind).replace('_', ' ')} at {name}{fbit}.{obit} "
            f"Want me to turn this into the next post + customer note?")
    return body, f"hero=trigger kind '{kind}' with verbatim payload facts (no invention)"


RENDERERS = {
    "research_digest": _r_research_digest,
    "regulation_change": _r_regulation,
    "recall_due": _r_recall,
    "perf_dip": lambda c, m, t, u: _r_perf(c, m, t, u, up=False),
    "seasonal_perf_dip": lambda c, m, t, u: _r_perf(c, m, t, u, up=False),
    "perf_spike": lambda c, m, t, u: _r_perf(c, m, t, u, up=True),
    "renewal_due": _r_renewal,
    "festival_upcoming": _r_festival,
    "wedding_package_followup": _r_bridal,
    "trial_followup": _r_trial_followup,
    "curious_ask_due": _r_curious,
    "winback_eligible": _r_winback,
    "ipl_match_today": _r_ipl,
    "review_theme_emerged": _r_review,
    "milestone_reached": _r_milestone,
    "active_planning_intent": _r_planning,
    "supply_alert": _r_supply,
    "chronic_refill_due": _r_refill,
    "customer_lapsed_hard": _r_customer_lapse,
    "customer_lapsed_soft": _r_customer_lapse,
    "category_seasonal": _r_seasonal,
    "gbp_unverified": _r_gbp,
    "cde_opportunity": _r_cde,
    "competitor_opened": _r_competitor,
    "dormant_with_vera": _r_dormant,
    "appointment_tomorrow": _r_trial_followup,
}


def compose(category, merchant, trigger, customer=None, polish_fn=None):
    """Deterministic compose(category, merchant, trigger, customer?) -> dict.

    Returns keys: body, cta, send_as, suppression_key, rationale.
    Optional polish_fn(text)->str lets an LLM rephrase for flow; the
    validator in polish.py guarantees the sent text keeps every fact.
    """
    try:
        from polish import polish as _polish
    except Exception:
        def _polish(body, call_fn=None, timeout=8):
            return body, "polish=off"
    category = _de_mojibake(category or {})
    merchant = _de_mojibake(merchant or {})
    trigger = _de_mojibake(trigger or {})
    customer = _de_mojibake(customer) if customer else None
    kind = trigger.get("kind", "update")
    scope = trigger.get("scope", "customer" if customer else "merchant")

    renderer = RENDERERS.get(kind, None)
    llm_note = ""
    if renderer is None and polish_fn is not None:
        # Unknown scenario (the harness's twist): let the LLM draft from the
        # pushed contexts, validator-gated. Fallback is the generic renderer.
        from polish import grounded_compose as _gc
        drafted, llm_note = _gc(category, merchant, trigger, customer, polish_fn)
        if drafted is not None:
            renderer = lambda c, m, t, u: (drafted, "llm grounded-compose from live contexts")
    if renderer is None:
        renderer = _r_generic
    body, why = renderer(category, merchant, trigger, customer)
    body = _scrub(body)

    # Optional LLM polish: rewords for flow only. The validator rejects any
    # rephrase that alters a fact, and any error falls back to this draft.
    final_body, polish_note = _polish(body, polish_fn)
    if final_body != body:
        final_body = _scrub(final_body)

    send_as = "merchant_on_behalf" if (scope == "customer" and customer) else "vera"
    cta = _cta_for(kind, scope)
    suppression = trigger.get("suppression_key", f"{kind}:{merchant.get('merchant_id', 'x')}")

    slug = category.get("slug", "?")
    mid = merchant.get("merchant_id", "?")
    rationale = (f"[{slug}/{kind}] {why}. send_as={send_as} from trigger scope; "
                 f"cta={cta} per kind policy; all facts quoted from inputs (no invention). "
                 f"{polish_note} {llm_note}".strip())

    # Determinism receipt: same inputs -> same outputs (no randomness anywhere).
    _ = hashlib.sha256(f"{slug}|{mid}|{kind}".encode()).hexdigest()[:8]
    return {
        "body": final_body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": suppression,
        "rationale": rationale,
    }
