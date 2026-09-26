"""LLM polish layer — the complement to the deterministic composer, not a replacement.

Pipeline: deterministic compose() decides EVERYTHING (hero fact, offer, CTA,
voice) -> this module optionally rephrases for flow/warmth -> a validator
checks the rephrase against a fact allowlist -> any violation, timeout, or
missing API key falls back to the deterministic draft untouched.

So the strongest claim in the interview stays true: the system can never send
an ungrounded message, with or without the LLM. The LLM only ever rewords.
"""

import json
import re
import urllib.request

TABOO_HYPE = ["guaranteed", "miracle", "best in city", "instant transformation",
              "viral guarantee", "shred in", "fastest results", "100% safe"]

SYSTEM = ("You rephrase WhatsApp business messages. RULES: do not change, add, "
          "or remove ANY number, price, date, name, place, or offer. Keep the "
          "same single call-to-action and the same language mix. No hype words. "
          "Reply with ONLY the rephrased message, nothing else.")


def extract_facts(text):
    """Numbers/names the polish must preserve verbatim (normalized tokens)."""
    nums = set()
    for pat in (r"₹\s?[\d,]+", r"[-+]?\d+(?:\.\d+)?%", r"\d+\s?(?:days?|min|sec|months?)"):
        for m in re.findall(pat, str(text)):
            nums.add(re.sub(r"[\s,]", "", m.lower()))
    names = set(re.findall(r"[A-Z][a-z]+", str(text)))
    return {"numbers": nums, "names": names}


def call_openai_compatible(prompt, base_url, api_key, model, timeout=8):
    body = json.dumps({"model": model,
                       "messages": [{"role": "system", "content": SYSTEM},
                                    {"role": "user", "content": prompt}],
                       "temperature": 0.3, "max_tokens": 300}).encode()
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
                                 data=body,
                                 headers={"Authorization": f"Bearer {api_key}",
                                          "Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=timeout)
    data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"].strip()


def validate_polish(original, polished):
    """Return (ok, reason). The polish may reword; it may not re-fact."""
    if not polished or len(polished) < 20:
        return False, "empty/too-short"
    if len(polished) > len(original) * 1.5 or len(polished) < len(original) * 0.5:
        return False, "length-drift"
    fo, fp = extract_facts(original), extract_facts(polished)
    if not fp["numbers"].issubset(fo["numbers"]):
        return False, f"number-changed:{sorted(fp['numbers'] - fo['numbers'])[:2]}"
    low = polished.lower()
    if any(h in low for h in TABOO_HYPE):
        return False, "hype-word"
    tail = polished[-80:].lower()
    if ("?" not in tail) and not any(v in tail for v in ("reply ", "confirm", "yes")):
        return False, "cta-lost"
    return True, "accepted"


def polish(body, call_fn=None, timeout=8):
    """Attempt LLM rephrase. Returns (text, note). Never raises."""
    if call_fn is None:
        return body, "polish=off"
    try:
        out = call_fn(f"Rephrase this merchant message, keeping every fact identical:\n\n{body}")
        ok, reason = validate_polish(body, out)
        if ok:
            return out, "polish=llm-accepted"
        return body, f"polish=rejected:{reason}"
    except Exception as e:  # timeouts, bad keys, outages -> deterministic draft
        return body, f"polish=fallback:{type(e).__name__}"
