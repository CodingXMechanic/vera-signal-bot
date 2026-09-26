#!/usr/bin/env python3
"""Exhaustive verification: composer x seeds + expanded data + fuzz + reply matrix.

Covers every trigger kind, every category, determinism, grounding, CTA policy,
reply state-machine branches, and hostile/degenerate inputs. No network needed
(except the live-server section, which is in test_local.py).
"""
import json
import io
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from composer import compose, RENDERERS
from reply_engine import reply

fails = []
n_checks = [0]


def check(name, cond, extra=""):
    n_checks[0] += 1
    if not cond:
        fails.append(name)
        print(f"FAIL {name} {extra}")
    return cond


SEED = HERE.parent / "magicpin-ai-challenge" / "dataset"
EXP = HERE.parent / "magicpin-ai-challenge" / "expanded"


def load(p, key):
    return {x[key]: x for x in json.load(open(p, encoding="utf-8"))[key]}


cats = {json.load(open(f, encoding="utf-8"))["slug"]: json.load(open(f, encoding="utf-8"))
        for f in (SEED / "categories").glob("*.json")}
merchs = load(SEED / "merchants_seed.json", "merchants") if False else {
    m["merchant_id"]: m for m in json.load(open(SEED / "merchants_seed.json", encoding="utf-8"))["merchants"]}
custs = {c["customer_id"]: c for c in json.load(open(SEED / "customers_seed.json", encoding="utf-8"))["customers"]}
trigs = json.load(open(SEED / "triggers_seed.json", encoding="utf-8"))["triggers"]

CTA_POLICY = {
    "recall_due": "multi_choice_slot", "trial_followup": "multi_choice_slot",
    "appointment_tomorrow": "multi_choice_slot", "chronic_refill_due": "multi_choice_slot",
    "wedding_package_followup": "multi_choice_slot",
    "curious_ask_due": "open_ended", "dormant_with_vera": "open_ended",
    "research_digest": "open_ended", "cde_opportunity": "open_ended",
}
BANNED = ["HIIT", "sub-potency", "Embassy", "RMZ", "10 thalis", "dosa platter",
          "-12%", "match-night combo", "'BOGO'", "45 min", "case-mix",
          "skin-prep + trial bookings peak", "increase your sales",
          "discount campaign", "guaranteed", "miracle", "best in city"]

# ---- 1. all 25 seed triggers: schema + determinism + grounding + CTA ----
for t in trigs:
    m = merchs[t["merchant_id"]]
    c = cats[m["category_slug"]]
    cu = custs.get(t["customer_id"]) if t.get("customer_id") else None
    a = compose(c, m, t, cu)
    b = compose(c, m, t, cu)
    tid = t["id"]
    check(f"{tid} schema", all(k in a for k in ("body", "cta", "send_as", "suppression_key", "rationale")))
    check(f"{tid} deterministic", a == b)
    check(f"{tid} body len", len(a["body"]) > 40, str(len(a["body"])))
    tail = a["body"][-80:].lower()
    # CTA may be a question ("?...?", "Want me to...?") or an imperative
    # ("Reply 1 or 2...", "Reply CONFIRM...") — both match the case studies.
    check(f"{tid} single CTA question",
          ("?" in tail) or any(v in tail for v in ("reply ", "confirm", "takes 5 min")),
          a["body"][-60:])
    check(f"{tid} no banned", not any(x in a["body"] for x in BANNED),
          str([x for x in BANNED if x in a["body"]]))
    check(f"{tid} suppression", a["suppression_key"] == t.get("suppression_key", tid))
    if t.get("scope") == "customer" and cu:
        check(f"{tid} send_as behalf", a["send_as"] == "merchant_on_behalf")
    else:
        check(f"{tid} send_as vera", a["send_as"] == "vera")
    if t["kind"] in CTA_POLICY and (t.get("scope") == "customer" or t["kind"] in
            ("curious_ask_due", "dormant_with_vera", "research_digest", "cde_opportunity",
             "wedding_package_followup") or True):
        pass  # policy checked below in scope-aware manner
    exp_cta = CTA_POLICY.get(t["kind"])
    if exp_cta == "multi_choice_slot" and t.get("scope") == "customer" and cu:
        check(f"{tid} cta slot", a["cta"] == "multi_choice_slot", a["cta"])
    if exp_cta == "open_ended":
        check(f"{tid} cta open", a["cta"] == "open_ended", a["cta"])

# ---- 2. renderer coverage: every kind the generator can emit ----
GEN_KINDS = ["research_digest", "perf_dip", "perf_spike", "milestone_reached",
             "dormant_with_vera", "review_theme_emerged", "competitor_opened",
             "festival_upcoming", "recall_due", "customer_lapsed_soft",
             "appointment_tomorrow", "chronic_refill_due", "trial_followup",
             "renewal_due", "curious_ask_due", "regulation_change",
             "wedding_package_followup", "winback_eligible", "ipl_match_today",
             "active_planning_intent", "seasonal_perf_dip", "customer_lapsed_hard",
             "supply_alert", "category_seasonal", "gbp_unverified", "cde_opportunity"]
for k in GEN_KINDS:
    check(f"renderer:{k}", k in RENDERERS)

# ---- 3. expanded dataset (100 triggers incl. placeholder payloads) ----
if EXP.exists():
    em, ec, et = {}, {}, []
    for f in (EXP / "merchants").glob("*.json"):
        d = json.load(open(f, encoding="utf-8")); em[d["merchant_id"]] = d
    for f in (EXP / "customers").glob("*.json"):
        d = json.load(open(f, encoding="utf-8")); ec[d["customer_id"]] = d
    for f in (EXP / "triggers").glob("*.json"):
        et.append(json.load(open(f, encoding="utf-8")))
    ecat = {json.load(open(f, encoding="utf-8"))["slug"]: json.load(open(f, encoding="utf-8"))
            for f in (EXP / "categories").glob("*.json")}
    ok = True
    for t in et:
        try:
            m = em.get(t.get("merchant_id", "")) or list(em.values())[0]
            c = ecat.get(m.get("category_slug", ""), {})
            cu = ec.get(t.get("customer_id", "")) if t.get("customer_id") else None
            a = compose(c, m, t, cu)
            assert all(k in a for k in ("body", "cta", "send_as", "suppression_key", "rationale"))
            assert len(a["body"]) > 20
        except Exception as e:  # noqa
            ok = False
            print(f"FAIL expanded:{t.get('id')} {e}")
            break
    check("expanded 100 triggers compose", ok, f"n={len(et)}")
else:
    print("SKIP expanded (run generate_dataset.py --out expanded first)")

# ---- 4. fuzz: degenerate inputs must never crash ----
m0 = merchs["m_001_drmeera_dentist_delhi"]
c0 = cats["dentists"]
FUZZ = [
    (c0, m0, {"kind": "totally_new_kind_xyz", "scope": "merchant", "payload": {"foo": "bar"}, "suppression_key": "x"}, None),
    (c0, m0, {"kind": "perf_dip", "scope": "merchant", "payload": {}, "suppression_key": "x"}, None),
    ({}, {}, {}, None),
    (c0, m0, {"kind": "recall_due", "scope": "customer", "payload": {}, "suppression_key": "x"}, None),
    (c0, {"merchant_id": "m_x"}, {"kind": "renewal_due", "scope": "merchant", "payload": {}, "suppression_key": "x"}, None),
    (cats["pharmacies"], merchs["m_010_sunrisepharm_pharmacy_lucknow"],
     {"kind": "chronic_refill_due", "scope": "customer", "payload": {"molecule_list": []}, "suppression_key": "x"},
     custs["c_015_anonymous_for_m010"]),
]
for i, (c, m, t, cu) in enumerate(FUZZ):
    try:
        a = compose(c, m, t, cu)
        check(f"fuzz{i} schema", all(k in a for k in ("body", "cta", "send_as", "suppression_key", "rationale")))
    except Exception as e:  # noqa
        check(f"fuzz{i} no-crash", False, repr(e)[:100])

# ---- 5. reply matrix ----
def C(**kw):
    d = {"turns": [], "auto_streak": 0, "nudges": 0, "ended": False,
         "trigger_id": "", "merchant_id": "m1", "topic": "research digest"}
    d.update(kw)
    return d


M = {"identity": {"owner_first_name": "Meera"}}
check("commit en", reply(C(), "Yes please send the abstract", M, "t")["action"] == "send")
check("commit en2", "CONFIRM" in reply(C(), "Ok lets do it. Whats next?", M, "t")["body"])
check("commit hi", reply(C(), "haan bhej do", M, "t")["cta"] == "binary_yes_no")
check("commit hi2", reply(C(), "theek hai, kar do", M, "t")["action"] == "send")
r = reply(C(turns=[{"from": "merchant", "msg": "ok"}, {"from": "vera", "msg": "x"}]), "ok", M, "t")
check("short repeat NOT autoreply", r["action"] == "send" and "auto-reply" not in r.get("body", "").lower())
r = reply(C(), "Thank you for contacting us! Our team will respond shortly.", M, "t")
check("auto1 send", r["action"] == "send")
c = C(auto_streak=1, turns=[{"from": "merchant", "msg": "Thank you for contacting us! Our team will respond shortly."}])
check("auto2 wait", reply(c, "Thank you for contacting us! Our team will respond shortly.", M, "t")["action"] == "wait")
check("auto3 end", reply(C(auto_streak=2), "Thank you for contacting us! Our team will respond shortly.", M, "t")["action"] == "end")
check("hostile end", reply(C(), "Stop messaging me. This is useless spam.", M, "t")["action"] == "end")
check("optout end", reply(C(), "not interested, unsubscribe me", M, "t")["action"] == "end")
r = reply(C(), "Can you help with GST filing?", M, "planning")
check("offtopic redirect", r["action"] == "send" and "CA" in r["body"])
check("stall wait", reply(C(), "busy now, later please", M, "t")["action"] == "wait")
check("nudge cap", reply(C(nudges=3), "hmm", M, "t")["action"] == "end")
check("ended sticks", reply(C(ended=True), "yes do it", M, "t")["action"] == "end")

# ---- 6. submission.jsonl ----
lines = [json.loads(l) for l in open(HERE / "submission.jsonl", encoding="utf-8")]
check("submission 25 lines", len(lines) == 25, str(len(lines)))
check("submission schema", all(all(k in d for k in ("test_id", "trigger_id", "merchant_id", "body", "cta",
      "send_as", "suppression_key", "rationale")) for d in lines))
check("submission bodies", all(len(d["body"]) > 40 for d in lines))
check("submission no banned", not any(x in d["body"] for d in lines for x in BANNED))

print(f"\n{n_checks[0] - len(fails)}/{n_checks[0]} checks passed")
print("ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
