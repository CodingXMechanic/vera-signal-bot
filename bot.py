#!/usr/bin/env python3
"""Vera bot — stdlib HTTP server (zero dependencies).

Endpoints: GET /v1/healthz, GET /v1/metadata,
           POST /v1/context, POST /v1/tick, POST /v1/reply
Run: python bot.py [--port 8080]
"""
import json
import time
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from composer import compose
from reply_engine import reply as reply_turn

START = time.time()
TEAM = {"team_name": "Vera Signal Bot", "team_members": ["Solo Builder"],
        "model": "deterministic-signal-arbitration-v2 (no LLM calls)",
        "approach": "signal-arbitration composer: one grounded hero fact per send, "
                    "per-kind renderers in per-category voice, multilingual reply state-machine "
                    "(auto-reply ladder, commit-flip, graceful exit)",
        "contact_email": "builder@example.com", "version": "2.0.0",
        "submitted_at": "2026-09-26T00:00:00Z"}

store = {}   # (scope, context_id) -> {"version": int, "payload": dict}
convos = {}  # conversation_id -> {"turns": [], "auto_streak": 0, "nudges": 0,
             #                     "ended": False, "trigger_id": str, "merchant_id": str}
sent_keys = set()  # suppression keys already emitted (dedup)


def _payload_of(scope, cid):
    e = store.get((scope, cid))
    return e["payload"] if e else None


def _tick(trg_ids, seen_conv):
    actions = []
    for tid in (trg_ids or [])[:20]:
        trg = _payload_of("trigger", tid)
        if not trg or not isinstance(trg, dict):
            continue
        mid = trg.get("merchant_id")
        merch = _payload_of("merchant", mid) if mid else None
        if not merch:
            continue
        cat = _payload_of("category", merch.get("category_slug", "")) or {}
        cust = _payload_of("customer", trg.get("customer_id")) if trg.get("customer_id") else None
        if trg.get("scope") == "customer" and not cust:
            continue  # never invent a customer message without its context
        skey = trg.get("suppression_key", tid)
        if skey in sent_keys:
            continue
        msg = compose(cat, merch, trg, cust)
        cid = trg.get("customer_id")
        conv = f"conv_{mid}_{tid}".replace(" ", "_")[:90] if not cid else f"conv_{cid}_{tid}".replace(" ", "_")[:90]
        if conv in seen_conv:
            continue
        seen_conv.add(conv)
        convos.setdefault(conv, {"turns": [], "auto_streak": 0, "nudges": 0,
                                 "ended": False, "trigger_id": tid, "merchant_id": mid,
                                 "topic": str(trg.get("kind", "")).replace("_", " ")})
        sent_keys.add(skey)
        actions.append({"conversation_id": conv, "merchant_id": mid,
                        "customer_id": cid, "send_as": msg["send_as"],
                        "trigger_id": tid, "template_name": f"vera_{trg.get('kind','generic')}_v1",
                        "template_params": [msg["body"][:60]],
                        "body": msg["body"], "cta": msg["cta"],
                        "suppression_key": msg["suppression_key"],
                        "rationale": msg["rationale"]})
        if len(actions) >= 20:
            break
    return actions


class H(BaseHTTPRequestHandler):
    server_version = "VeraBot/2.0"

    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _read(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except Exception:
            n = 0
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode() or "{}")
        except Exception:
            return {}

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/":
            self._send(200, {"service": "vera-signal-bot", "status": "live",
                             "judge_endpoints": ["GET /v1/healthz", "GET /v1/metadata",
                                                 "POST /v1/context", "POST /v1/tick",
                                                 "POST /v1/reply"],
                             "repo": "https://github.com/CodingXMechanic/vera-signal-bot"})
        elif p == "/v1/healthz":
            counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
            for (s, _) in store:
                if s in counts:
                    counts[s] += 1
            self._send(200, {"status": "ok", "uptime_seconds": int(time.time() - START),
                             "contexts_loaded": counts})
        elif p == "/v1/metadata":
            self._send(200, TEAM)
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self):
        from datetime import datetime, timezone
        try:
            self._do_post(datetime.now(timezone.utc).isoformat())
        except Exception as e:
            try:
                self._send(200, {"action": "wait", "wait_seconds": 3600,
                                 "rationale": f"recovered from internal error: {type(e).__name__}"})
            except Exception:
                pass

    def _do_post(self, now):
        from datetime import datetime, timezone  # noqa
        p = urlparse(self.path).path
        if p == "/v1/context":
            b = self._read()
            scope, cid, ver = b.get("scope"), b.get("context_id"), b.get("version")
            if scope not in ("category", "merchant", "customer", "trigger") or not cid or not isinstance(ver, int):
                self._send(400, {"accepted": False, "reason": "invalid_scope",
                                 "details": "need scope, context_id, int version"})
                return
            cur = store.get((scope, cid))
            if cur and cur["version"] >= ver:
                self._send(409, {"accepted": False, "reason": "stale_version",
                                 "current_version": cur["version"]})
                return
            store[(scope, cid)] = {"version": ver, "payload": b.get("payload", {})}
            self._send(200, {"accepted": True, "ack_id": f"ack_{cid}_v{ver}", "stored_at": now})
        elif p == "/v1/tick":
            b = self._read()
            acts = _tick(b.get("available_triggers", []), set())
            self._send(200, {"actions": acts})
        elif p == "/v1/reply":
            b = self._read()
            conv_id = b.get("conversation_id", "conv_unknown")
            c = convos.setdefault(conv_id, {"turns": [], "auto_streak": 0, "nudges": 0,
                                            "ended": False, "trigger_id": "",
                                            "merchant_id": b.get("merchant_id", ""),
                                            "topic": ""})
            merch = _payload_of("merchant", b.get("merchant_id", "") or c.get("merchant_id", "")) or {}
            if not c.get("topic") and c.get("trigger_id"):
                t = _payload_of("trigger", c["trigger_id"]) or {}
                c["topic"] = str(t.get("kind", "")).replace("_", " ")
            out = reply_turn(c, b.get("message", ""), merch, c.get("topic", ""))
            c["turns"].append({"from": b.get("from_role", "merchant"), "msg": b.get("message", "")})
            if out["action"] == "send":
                c["turns"].append({"from": "vera", "msg": out.get("body", "")})
                self._send(200, {"action": "send", "body": out["body"], "cta": out.get("cta", "open_ended"),
                                 "rationale": out.get("rationale", "")})
            elif out["action"] == "wait":
                self._send(200, {"action": "wait", "wait_seconds": out.get("wait_seconds", 3600),
                                 "rationale": out.get("rationale", "")})
            else:
                self._send(200, {"action": "end", "rationale": out.get("rationale", "")})
        else:
            self._send(404, {"error": "not_found"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    import os
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), H)
    print(f"Vera bot live on {a.host}:{a.port}", flush=True)
    srv.serve_forever()
