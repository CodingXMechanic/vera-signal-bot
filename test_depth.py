#!/usr/bin/env python3
"""Depth battery: 25 adversarial merchant messages + live end-to-end flows.

Runs its own bot instance in-thread on 127.0.0.1:8094, leaving the
submission instance on :8080 pristine.
"""
import io
import json
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from bot import H
from http.server import ThreadingHTTPServer

PORT = 8094
B = f"http://127.0.0.1:{PORT}"
srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.5)

fails = []
n = [0]


def check(name, cond, extra=""):
    n[0] += 1
    if not cond:
        fails.append(name)
        print(f"FAIL {name} {extra}")


def call(method, path, body=None):
    req = urllib.request.Request(
        B + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method, headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


SEED = HERE.parent / "magicpin-ai-challenge" / "dataset"
cats = [json.load(open(f, encoding="utf-8")) for f in (SEED / "categories").glob("*.json")]
merchs = json.load(open(SEED / "merchants_seed.json", encoding="utf-8"))["merchants"]
trigs = json.load(open(SEED / "triggers_seed.json", encoding="utf-8"))["triggers"]
custs = json.load(open(SEED / "customers_seed.json", encoding="utf-8"))["customers"]
for c in cats:
    call("POST", "/v1/context", {"scope": "category", "context_id": c["slug"], "version": 1,
                                 "payload": c, "delivered_at": "x"})
for m in merchs:
    call("POST", "/v1/context", {"scope": "merchant", "context_id": m["merchant_id"], "version": 1,
                                 "payload": m, "delivered_at": "x"})
for cu in custs:
    call("POST", "/v1/context", {"scope": "customer", "context_id": cu["customer_id"], "version": 1,
                                 "payload": cu, "delivered_at": "x"})
for t in trigs:
    call("POST", "/v1/context", {"scope": "trigger", "context_id": t["id"], "version": 1,
                                 "payload": t, "delivered_at": "x"})
MID = merchs[0]["merchant_id"]

# ---- 1. adversarial message battery (fresh convo each) ----
BATTERY = [
    # (message, expected_action, must_contain_or_None)
    ("Yes please send the abstract", "send", "CONFIRM"),
    ("Ok lets do it. Whats next?", "send", "CONFIRM"),
    ("haan bhej do", "send", "CONFIRM"),
    ("theek hai, kar do", "send", "CONFIRM"),
    ("bilkul, proceed karo", "send", "CONFIRM"),
    ("YES", "send", None),                       # bare yes -> default advance, must NOT end
    ("Confirm. Go ahead.", "send", "CONFIRM"),
    ("send me the list please", "send", None),
    ("what would it look like", "send", None),
    ("ok", "send", None),
    ("hmm", "send", None),
    ("maybe later", "wait", None),
    ("busy now, call later", "wait", None),
    ("baad me baat karte hain", "wait", None),
    ("Thank you for contacting us! Our team will respond shortly.", "send", "auto-reply"),
    ("Sampark karne ke liye dhanyavad! Hamari team jald sampark karegi", "send", None),
    ("Stop messaging me. This is useless spam.", "end", None),
    ("band karo, bakwas hai", "end", None),
    ("STOP", "send", None),                       # bare STOP w/o hostility -> advance, not end
    ("Can you help with GST filing?", "send", "CA"),
    ("need a loan for shop renovation?", "send", "CA"),
    ("what is the price?", "send", None),
    ("asdfgh", "send", None),
    ("", "send", None),
    ("This is great! " * 40, "send", None),       # very long message
]
for i, (msg, exp, needle) in enumerate(BATTERY):
    s, r = call("POST", "/v1/reply", {"conversation_id": f"cx_bat_{i}", "merchant_id": MID,
                                      "from_role": "merchant", "message": msg,
                                      "received_at": "x", "turn_number": 2})
    ok = s == 200 and r.get("action") == exp
    if needle and ok and exp == "send":
        ok = needle.lower() in r.get("body", "").lower()
    check(f"batt[{msg[:28]}]->{exp}", ok, str(r)[:110])

# short repeat twice must NOT be treated as auto-reply
s, r1 = call("POST", "/v1/reply", {"conversation_id": "cx_rep", "merchant_id": MID,
                                   "from_role": "merchant", "message": "ok",
                                   "received_at": "x", "turn_number": 2})
s, r2 = call("POST", "/v1/reply", {"conversation_id": "cx_rep", "merchant_id": MID,
                                   "from_role": "merchant", "message": "ok",
                                   "received_at": "x", "turn_number": 3})
check("repeat-ok not autoreply", "auto-reply" not in r2.get("body", "").lower(), str(r2)[:100])

# auto ladder across three identical canned turns
for tn, exp in ((2, "send"), (3, "wait"), (4, "end")):
    s, r = call("POST", "/v1/reply", {"conversation_id": "cx_lad", "merchant_id": MID,
                                      "from_role": "merchant",
                                      "message": "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly.",
                                      "received_at": "x", "turn_number": tn})
    check(f"ladder t{tn}->{exp}", r.get("action") == exp, str(r)[:90])

# ---- 2. end-to-end customer recall flow ----
s, r = call("POST", "/v1/tick", {"now": "x", "available_triggers": ["trg_003_recall_due_priya"]})
acts = r.get("actions", [])
check("recall tick 1 action", len(acts) == 1, str(len(acts)))
if acts:
    a = acts[0]
    check("recall behalf", a["send_as"] == "merchant_on_behalf")
    check("recall slot cta", a["cta"] == "multi_choice_slot", a["cta"])
    check("recall names priya", "Priya" in a["body"] and "299" in a["body"])
    s, r = call("POST", "/v1/reply", {"conversation_id": a["conversation_id"],
                                      "merchant_id": a["merchant_id"], "customer_id": a["customer_id"],
                                      "from_role": "customer", "message": "1, Wednesday works",
                                      "received_at": "x", "turn_number": 2})
    check("customer reply handled", s == 200 and r.get("action") in ("send", "wait", "end"), str(r)[:90])

# ---- 3. suppression: re-tick same triggers -> silence ----
s, r = call("POST", "/v1/tick", {"now": "x", "available_triggers": [t["id"] for t in trigs]})
n1 = len(r.get("actions", []))
s, r = call("POST", "/v1/tick", {"now": "x", "available_triggers": [t["id"] for t in trigs]})
check("retick suppressed", len(r.get("actions", [])) == 0, f"first={n1}")
check("first tick capped", n1 <= 20, str(n1))

# ---- 4. version bump replaces; unknown trigger ids ignored ----
m0 = dict(merchs[0])
s, r = call("POST", "/v1/context", {"scope": "merchant", "context_id": m0["merchant_id"],
                                    "version": 2, "payload": m0, "delivered_at": "x"})
check("version bump accepted", s == 200 and r.get("accepted") is True)
s, r = call("POST", "/v1/tick", {"now": "x", "available_triggers": ["nope_123", "bogus"]})
check("unknown triggers ignored", s == 200 and r.get("actions") == [])

srv.shutdown()
print(f"\n{n[0] - len(fails)}/{n[0]} depth checks passed")
print("ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
