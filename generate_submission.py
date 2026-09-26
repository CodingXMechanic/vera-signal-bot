#!/usr/bin/env python3
"""Build submission.jsonl (one line per seed trigger) using the composer directly."""
import json
from pathlib import Path
from composer import compose

BASE = Path(__file__).parent
SEED = BASE.parent / "magicpin-ai-challenge" / "dataset"

cats = {json.load(open(f, encoding="utf-8"))["slug"]: json.load(open(f, encoding="utf-8")) for f in (SEED / "categories").glob("*.json")}
merchs = {m["merchant_id"]: m for m in json.load(open(SEED / "merchants_seed.json", encoding="utf-8"))["merchants"]}
custs = {c["customer_id"]: c for c in json.load(open(SEED / "customers_seed.json", encoding="utf-8"))["customers"]}
trigs = json.load(open(SEED / "triggers_seed.json", encoding="utf-8"))["triggers"]

out = BASE / "submission.jsonl"
with open(out, "w", encoding="utf-8") as f:
    for i, t in enumerate(trigs, 1):
        m = merchs[t["merchant_id"]]
        c = cats[m["category_slug"]]
        cu = custs.get(t["customer_id"]) if t.get("customer_id") else None
        msg = compose(c, m, t, cu)
        f.write(json.dumps({"test_id": f"T{i:02d}", "trigger_id": t["id"],
                            "merchant_id": t["merchant_id"],
                            "customer_id": t.get("customer_id"), **msg}, ensure_ascii=False) + "\n")
print(f"wrote {len(trigs)} lines -> {out}")
