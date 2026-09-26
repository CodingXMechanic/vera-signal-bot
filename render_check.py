#!/usr/bin/env python3
"""Hosted-instance verification for the Render deploy. Writes UTF-8 log."""
import io
import json
import sys
import urllib.request
import urllib.error

out = open("render_check_out.txt", "w", encoding="utf-8")
B = "https://vera-signal-bot.onrender.com"


def log(*a):
    print(*a, file=out)


def call(method, path, body=None):
    req = urllib.request.Request(
        B + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method, headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=40)
        return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


fails = []
try:
    s, h = call("GET", "/v1/healthz")
    log("healthz:", s, h)
    if s != 200 or h.get("status") != "ok":
        fails.append("healthz")
    s, md = call("GET", "/v1/metadata")
    log("metadata:", s, md.get("version"), md.get("team_name"))
    if s != 200 or md.get("version") != "2.2.0":
        fails.append("metadata-version")

    seed = "../magicpin-ai-challenge/dataset/"
    c = json.load(open(seed + "categories/dentists.json", encoding="utf-8"))
    ms = json.load(open(seed + "merchants_seed.json", encoding="utf-8"))["merchants"]
    m = [x for x in ms if x["merchant_id"] == "m_001_drmeera_dentist_delhi"][0]
    ts = json.load(open(seed + "triggers_seed.json", encoding="utf-8"))["triggers"]
    t = [x for x in ts if x["id"] == "trg_001_research_digest_dentists"][0]
    cs = json.load(open(seed + "customers_seed.json", encoding="utf-8"))["customers"]
    cu = [x for x in cs if x["customer_id"] == "c_001_priya_for_m001"][0]
    t3 = [x for x in ts if x["id"] == "trg_003_recall_due_priya"][0]
    for scope, cid, p in [("category", "dentists", c), ("merchant", m["merchant_id"], m),
                          ("customer", cu["customer_id"], cu),
                          ("trigger", t["id"], t), ("trigger", t3["id"], t3)]:
        s, r = call("POST", "/v1/context", {"scope": scope, "context_id": cid,
                                            "version": 1, "payload": p, "delivered_at": "x"})
        log("ctx", cid, s)
        if s != 200:
            fails.append(f"ctx-{cid}")

    s, r = call("POST", "/v1/tick", {"now": "2026-04-26T10:35:00Z",
                                     "available_triggers": ["trg_001_research_digest_dentists",
                                                            "trg_003_recall_due_priya"]})
    acts = r.get("actions", [])
    log("tick:", s, len(acts))
    if s != 200 or len(acts) != 2:
        fails.append(f"tick-actions={len(acts)}")
    for a in acts:
        log("-", a["send_as"], "|", a["cta"], "|", a["body"][:110])
        if not all(k in a for k in ("conversation_id", "merchant_id", "send_as",
                                    "trigger_id", "body", "cta", "suppression_key", "rationale")):
            fails.append("tick-schema")
        s2, r2 = call("POST", "/v1/reply",
                      {"conversation_id": a["conversation_id"], "merchant_id": a["merchant_id"],
                       "customer_id": a.get("customer_id"), "from_role": "merchant",
                       "message": "Yes please, go ahead",
                       "received_at": "x", "turn_number": 2})
        log("  reply:", s2, r2.get("action"), str(r2.get("body", r2.get("rationale", "")))[:85])
        if s2 != 200 or r2.get("action") != "send":
            fails.append("reply-send")
    kinds = {(a["send_as"], a["cta"]) for a in acts}
    if ("vera", "open_ended") not in kinds or ("merchant_on_behalf", "multi_choice_slot") not in kinds:
        fails.append(f"tick-variety={kinds}")
except Exception as e:  # noqa
    log("ERROR", repr(e)[:200])
    fails.append("exception")

log("RESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
out.close()
print("RESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
