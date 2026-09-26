#!/usr/bin/env python3
"""Adaptive-injection simulation: the harness's fresh-scenario twist, rehearsed."""
import io
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from composer import compose

fails = []
n = [0]


def check(name, cond, extra=""):
    n[0] += 1
    if not cond:
        fails.append(name)
        print(f"FAIL {name} {extra}")


SD = HERE.parent / "magicpin-ai-challenge" / "dataset"
cats = {json.load(open(SD / "categories" / f, encoding="utf-8"))["slug"]:
        json.load(open(SD / "categories" / f, encoding="utf-8"))
        for f in ["pharmacies.json", "dentists.json", "gyms.json"]}
merchs = {m["merchant_id"]: m for m in
          json.load(open(SD / "merchants_seed.json", encoding="utf-8"))["merchants"]}


def fresh(obj):
    return json.loads(json.dumps(obj))


# 1. brand-new digest item the bot never saw in development
cat = fresh(cats["pharmacies"])
cat["digest"].insert(0, {"id": "d_NEW_mrna_flu", "kind": "research",
                         "title": "mRNA flu shots cut senior hospitalisation 41% this season",
                         "source": "ICMR bulletin Nov 2026", "trial_n": 8500,
                         "patient_segment": "seniors_65plus", "summary": "x"})
m = fresh(merchs["m_009_apollo_pharmacy_jaipur"])
a = compose(cat, m, {"kind": "research_digest", "scope": "merchant",
                     "payload": {"top_item_id": "d_NEW_mrna_flu"},
                     "suppression_key": "new:test:1"}, None)
check("new-digest-cited", "41%" in a["body"] and "ICMR" in a["body"]
      and ("8,500" in a["body"] or "8500" in a["body"]), a["body"][:120])

# 2. shifted metrics mid-test
m2 = fresh(merchs["m_002_bharat_dentist_mumbai"])
m2["performance"] = {"window_days": 30, "views": 3120, "calls": 22,
                     "directions": 60, "ctr": 0.028, "delta_7d": {"views_pct": 0.42}}
a = compose(cats["dentists"], m2,
            {"kind": "perf_spike", "scope": "merchant",
             "payload": {"metric": "views", "delta_pct": 0.42, "window": "7d"},
             "suppression_key": "new:test:2"}, None)
check("shifted-metrics-used", "42%" in a["body"], a["body"][:100])

# 3. entirely new trigger kind, unseen shape
a = compose(cats["dentists"], fresh(merchs["m_001_drmeera_dentist_delhi"]),
            {"kind": "monsoon_flood_alert", "scope": "merchant",
             "payload": {"area": "Lajpat Nagar", "rain_mm": 180,
                         "clinic_action": "move records upstairs"},
             "suppression_key": "new:test:3"}, None)
check("unknown-kind-grounded", "180" in a["body"] and "Lajpat Nagar" in a["body"]
      and len(a["body"]) > 40, a["body"][:120])

# 4. surprise customer appearing mid-test
cu = {"customer_id": "c_NEW", "merchant_id": "m_007_powerhouse_gym_bangalore",
      "identity": {"name": "Farhan", "language_pref": "hi-en mix"},
      "relationship": {"last_visit": "2026-01-15"}, "state": "lapsed_hard",
      "preferences": {"preferred_slots": "weekday_morning"}, "consent": {}}
a = compose(cats["gyms"], fresh(merchs["m_007_powerhouse_gym_bangalore"]),
            {"kind": "customer_lapsed_hard", "scope": "customer",
             "payload": {"days_since_last_visit": 120, "previous_focus": "marathon_training"},
             "suppression_key": "new:test:4"}, cu)
check("surprise-customer", a["send_as"] == "merchant_on_behalf" and "Farhan" in a["body"]
      and "marathon training" in a["body"] and "HIIT" not in a["body"], a["body"][:130])

# 5. category version bump mid-test: new item must win over old top item
cat2 = fresh(cats["dentists"])
old_top = cat2["digest"][0]["id"]
cat2["digest"].insert(0, {"id": "d_NEW2", "kind": "research",
                          "title": "Night-guard compliance doubles with morning SMS reminders",
                          "source": "JIDA Nov 2026, p.31", "trial_n": 640,
                          "patient_segment": "bruxism_adults", "summary": "x"})
a = compose(cat2, fresh(merchs["m_001_drmeera_dentist_delhi"]),
            {"kind": "research_digest", "scope": "merchant",
             "payload": {"top_item_id": "d_NEW2"}, "suppression_key": "new:test:5"}, None)
check("version-bump-wins", "Night-guard" in a["body"] and "p.31" in a["body"]
      and old_top not in a["body"], a["body"][:130])

print(f"\n{n[0] - len(fails)}/{n[0]} injection checks passed")
print("ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
