#!/usr/bin/env python3
"""Polish-layer tests: fake LLM provider, real validator. No network."""
import io
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from polish import polish, validate_polish
from composer import compose
import json

fails = []
n = [0]


def check(name, cond, extra=""):
    n[0] += 1
    if not cond:
        fails.append(name)
        print(f"FAIL {name} {extra}")


ORIG = ("Hi Suresh, DC vs MI at Arun Jaitley Stadium tonight. Your calls are "
        "down 50% this week. Want me to draft the delivery banner? Live in 10 min.")
GOOD = ("Hi Suresh! DC vs MI is at Arun Jaitley Stadium tonight, and your calls "
        "are down 50% this week. Shall I draft the delivery banner? Live in 10 min.")

check("off by default", polish(ORIG) == (ORIG, "polish=off"))
check("good accepted", polish(ORIG, lambda p: GOOD)[0] == GOOD)
t, note = polish(ORIG, lambda p: GOOD)
check("note accepted", note == "polish=llm-accepted", note)
t, note = polish(ORIG, lambda p: GOOD.replace("50%", "60%"))
check("changed number rejected", t == ORIG and "number-changed" in note, note)
t, note = polish(ORIG, lambda p: GOOD + " Guaranteed packed house!")
check("hype rejected", t == ORIG and "hype-word" in note, note)
t, note = polish(ORIG, lambda p: GOOD.replace("Shall I draft the delivery banner? Live in 10 min.",
                                                 "The delivery banner is something I can draft. Live in 10 min."))
check("cta lost rejected", t == ORIG and "cta-lost" in note, note)


def boom(prompt):
    raise TimeoutError("slow api")


t, note = polish(ORIG, boom)
check("timeout fallback", t == ORIG and note.startswith("polish=fallback"), note)

# compose() integration
SEED = HERE.parent / "magicpin-ai-challenge" / "dataset"
m = [x for x in json.load(open(SEED / "merchants_seed.json", encoding="utf-8"))["merchants"]
     if x["merchant_id"] == "m_005_pizzajunction_restaurant_delhi"][0]
c = json.load(open(SEED / "categories" / "restaurants.json", encoding="utf-8"))
t10 = [t for t in json.load(open(SEED / "triggers_seed.json", encoding="utf-8"))["triggers"]
       if t["id"] == "trg_010_ipl_match_delhi"][0]
a = compose(c, m, t10)
check("default polish=off note", "polish=off" in a["rationale"], a["rationale"][-40:])
def good_rephrase(prompt):
    msg = prompt.split("\n\n", 1)[1]  # real providers also get prompt; reply is the message
    return msg.replace("Hi Suresh", "Hi Suresh!")


b = compose(c, m, t10, polish_fn=good_rephrase)
check("compose accepts good polish", b["body"].startswith("Hi Suresh!") and "llm-accepted" in b["rationale"])
check("cta unchanged by polish", b["cta"] == a["cta"] and b["send_as"] == a["send_as"])
d = compose(c, m, t10, polish_fn=lambda p: GOOD.replace("50%", "60%"))
check("compose falls back on bad polish", d["body"] == a["body"] and "rejected" in d["rationale"])

# ---- grounded unknown-scenario compose ----
UNK = {"kind": "monsoon_flood_alert", "scope": "merchant",
       "payload": {"area": "Lajpat Nagar", "rain_mm": 180,
                   "clinic_action": "move records upstairs"},
       "suppression_key": "u:test"}
M1 = {"merchant_id": "m_001_drmeera_dentist_delhi",
      "identity": {"name": "Dr. Meera's Dental Clinic", "city": "Delhi",
                   "locality": "Lajpat Nagar", "owner_first_name": "Meera"},
      "offers": []}
C1 = {"slug": "dentists", "voice": {}, "digest": [], "offer_catalog": []}


def good_llm(prompt, system=None):
    return ("Hi Meera, 180mm rain in Lajpat Nagar — move records upstairs today. "
            "Want me to draft the closure notice for patients?")


def bad_llm(prompt, system=None):
    return ("Hi Meera, GUARANTEED miracle fix! 50% off mega deal today only, "
            "plus a free car! Reply now now now.")


def dead_llm(prompt, system=None):
    raise TimeoutError("slow")


g = compose(C1, M1, UNK, polish_fn=good_llm)
check("unknown llm accepted", "llm-compose=accepted" in g["rationale"]
      and "180" in g["body"] and g["cta"] == "open_ended"
      and g["send_as"] == "vera", g["rationale"][-60:])
b = compose(C1, M1, UNK, polish_fn=bad_llm)
check("unknown inventing falls back", "rejected" in b["rationale"]
      and "180" in b["body"] and "miracle" not in b["body"].lower())
d = compose(C1, M1, UNK, polish_fn=dead_llm)
check("unknown timeout falls back", "fallback" in d["rationale"] and "180" in d["body"])
nn = compose(C1, M1, UNK)
check("unknown no-llm generic", "polish=off" in nn["rationale"] and "180" in nn["body"])

print(f"\n{n[0] - len(fails)}/{n[0]} polish checks passed")
print("ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
