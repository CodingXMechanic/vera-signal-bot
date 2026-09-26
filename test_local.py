#!/usr/bin/env python3
"""Lean local self-test: boots bot in-process thread, runs warmup+tick+reply checks."""
import json
import threading
import time
import urllib.request
import urllib.error
import faulthandler
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bot import H
from http.server import ThreadingHTTPServer

PORT = 8091
B = f"http://127.0.0.1:{PORT}"
srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.5)


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


fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name, extra)
    if not cond:
        fails.append(name)


seed = Path(__file__).parent.parent / "magicpin-ai-challenge" / "dataset"
cats = [json.load(open(f)) for f in (seed / "categories").glob("*.json")]
merchs = json.load(open(seed / "merchants_seed.json"))["merchants"]
trigs = json.load(open(seed / "triggers_seed.json"))["triggers"]
custs = json.load(open(seed / "customers_seed.json"))["customers"]

s, r = call("GET", "/v1/healthz")
check("healthz", s == 200 and r["status"] == "ok", str(r["contexts_loaded"]))
s, r = call("GET", "/v1/metadata")
check("metadata", s == 200 and "team_name" in r)
for c in cats:
    s, _ = call("POST", "/v1/context", {"scope": "category", "context_id": c["slug"],
                                        "version": 1, "payload": c, "delivered_at": "x"})
    check(f"context category/{c['slug']}", s == 200)
for m in merchs:
    s, _ = call("POST", "/v1/context", {"scope": "merchant", "context_id": m["merchant_id"],
                                        "version": 1, "payload": m, "delivered_at": "x"})
    check(f"context merchant/{m['merchant_id'][:12]}", s == 200)
s, r = call("POST", "/v1/context", {"scope": "merchant", "context_id": merchs[0]["merchant_id"],
                                    "version": 1, "payload": merchs[0], "delivered_at": "x"})
check("idempotent stale_version=409", s == 409 and r.get("reason") == "stale_version")
for cu in custs:
    call("POST", "/v1/context", {"scope": "customer", "context_id": cu["customer_id"],
                                 "version": 1, "payload": cu, "delivered_at": "x"})
for t in trigs:
    call("POST", "/v1/context", {"scope": "trigger", "context_id": t["id"],
                                 "version": 1, "payload": t, "delivered_at": "x"})
s, r = call("GET", "/v1/healthz")
check("healthz counts", r["contexts_loaded"] == {"category": 5, "merchant": 10,
      "customer": 15, "trigger": 25}, str(r["contexts_loaded"]))

s, r = call("POST", "/v1/tick", {"now": "2026-04-26T10:35:00Z",
                                 "available_triggers": [t["id"] for t in trigs]})
acts = r.get("actions", [])
check("tick returns actions (<=20)", s == 200 and 0 < len(acts) <= 20, f"n={len(acts)}")
ok_schema = all(all(k in a for k in ("conversation_id", "merchant_id", "send_as",
                   "trigger_id", "body", "cta", "suppression_key", "rationale")) for a in acts)
check("tick action schema", ok_schema)
bodies = [a["body"] for a in acts]
check("no generic copy", not any("increase your sales" in b.lower() or "discount campaign" in b.lower() for b in bodies))
check("merchant-facing send_as=vera present", any(a["send_as"] == "vera" for a in acts))
check("customer-facing send_as=merchant_on_behalf present",
      any(a["send_as"] == "merchant_on_behalf" for a in acts))

conv, mid = acts[0]["conversation_id"], acts[0]["merchant_id"]
s, r = call("POST", "/v1/reply", {"conversation_id": conv, "merchant_id": mid,
                                  "from_role": "merchant", "message": "Yes please send the abstract",
                                  "received_at": "x", "turn_number": 2})
check("reply commit -> send/action", s == 200 and r.get("action") == "send", str(r)[:100])

for i, tn in (("auto1", 2), ("auto2", 3), ("auto3", 4)):
    s, r = call("POST", "/v1/reply", {"conversation_id": "cx_auto", "merchant_id": mid,
                                      "from_role": "merchant",
                                      "message": "Thank you for contacting us! Our team will respond shortly.",
                                      "received_at": "x", "turn_number": tn})
    if i == "auto1":
        check("auto-reply 1st -> send(flag)", r.get("action") == "send", str(r)[:90])
    elif i == "auto2":
        check("auto-reply 2nd -> wait", r.get("action") == "wait", str(r)[:90])
    else:
        check("auto-reply 3rd -> end", r.get("action") == "end", str(r)[:90])

s, r = call("POST", "/v1/reply", {"conversation_id": "cx_int", "merchant_id": mid,
                                  "from_role": "merchant", "message": "Ok lets do it. Whats next?",
                                  "received_at": "x", "turn_number": 3})
check("intent transition -> action not qualifying",
      "CONFIRM" in r.get("body", "") or "draft" in r.get("body", "").lower(), r.get("body", "")[:90])

s, r = call("POST", "/v1/reply", {"conversation_id": "cx_hos", "merchant_id": mid,
                                  "from_role": "merchant", "message": "Stop messaging me. This is useless spam.",
                                  "received_at": "x", "turn_number": 2})
check("hostile -> end", r.get("action") == "end", str(r)[:90])

s, r = call("POST", "/v1/reply", {"conversation_id": "cx_off", "merchant_id": mid,
                                  "from_role": "merchant", "message": "Btw can you help with GST filing?",
                                  "received_at": "x", "turn_number": 2})
check("off-topic -> redirect send", r.get("action") == "send" and "CA" in r.get("body", ""), r.get("body", "")[:90])

srv.shutdown()
print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILURES: {fails}'}")
sys.exit(1 if fails else 0)
