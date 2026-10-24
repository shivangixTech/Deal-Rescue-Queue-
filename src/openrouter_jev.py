"""Jev decisions via OpenRouter (backup when TypeSafe signup is stuck).

Why this exists: TypeSafe's own API needs an early-access key, but OpenRouter
serves the same Jev model on a dedicated Decisions endpoint with an ordinary
OpenRouter key. Request and answer shapes mirror src/jev_client.py, so the
queue UI, policy routing, and logging consume the result unchanged.

Signup: openrouter.ai -> sign in -> "Keys" -> Create Key. OpenRouter is
prepaid, so add a few dollars of credits first. Set it in the local
environment as OPENROUTER_API_KEY (never paste it into chat).

Pin the model version: OpenRouter's pinned Jev release is
"typesafe/jev-1.13". Never use "jev-latest" here -- decision thresholds do
not transfer between model versions.

Stdlib only: no new dependency is added to requirements.txt.
"""

import json
import os
import time
import urllib.error
import urllib.request
import uuid

from .questions import QUESTION_VERSION, RISK_LEVELS

ENDPOINT = os.environ.get(
    "OPENROUTER_JEV_ENDPOINT", "https://openrouter.ai/api/alpha/decisions"
)
MODEL = os.environ.get("OPENROUTER_JEV_MODEL", "typesafe/jev-1.13")

# Jev's documented transient errors: 429 (rate limit), 529 (provider
# overloaded). We never retry anything else (401/422 are permanent).
_TRANSIENT_STATUS = (429, 529, 503)
_RETRY_ATTEMPTS = 3          # first call + up to 2 retries
_RETRY_BASE_SECS = 1.0       # backoff doubles each time: 1s, 2s


def _to_or_questions(spec):
    """Convert our {name: {kind, instructions, criteria}} spec to the
    Decisions-API question format {name: {type, instructions, criteria}}."""
    out = {}
    for name, q in spec.items():
        out[name] = {
            "type": q["kind"],            # kind already is noul|choice|score
            "instructions": q["instructions"],
        }
        if "criteria" in q:
            out[name]["criteria"] = q["criteria"]
    return out


def _post(body, api_key):
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/shivangixTech/deal-rescue-demo",
            "X-Title": "Deal Rescue Queue",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_with_retry(body, api_key):
    last = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return _post(body, api_key)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in _TRANSIENT_STATUS or attempt == _RETRY_ATTEMPTS - 1:
                raise
            time.sleep(_RETRY_BASE_SECS * (2 ** attempt))
    raise last  # unreachable, but keeps the type checker honest


def _pick_score_level(answer, levels):
    """Map a score answer to one of our level labels. The API returns a
    probability-weighted (possibly fractional) index plus a legend and a
    probability map keyed by index string; the most probable level wins."""
    probs = answer.get("probabilities") or {}
    if probs:
        best_idx = max(range(len(levels)), key=lambda i: probs.get(str(i), 0.0))
    else:
        best_idx = int(round(float(answer.get("score", 0))))
    best_idx = max(0, min(len(levels) - 1, best_idx))
    return levels[best_idx]


def decide(state, spec=None):
    """Run the deal-rescue Jev questions through OpenRouter. Never raises;
    failures return the same error-shaped dict as jev_client.decide."""
    from .questions import question_spec

    spec = spec or question_spec()
    correlation_id = uuid.uuid4().hex[:12]
    t0 = time.time()
    api_key = os.environ.get("OPENROUTER_API_KEY")

    def elapsed():
        return int((time.time() - t0) * 1000)

    if not api_key:
        return {
            "mode": "error",
            "model": MODEL,
            "correlation_id": correlation_id,
            "question_version": QUESTION_VERSION,
            "latency_ms": elapsed(),
            "error": "OPENROUTER_API_KEY is not set.",
        }

    body = {
        "model": MODEL,
        "state": state,
        "questions": _to_or_questions(spec),
        # Deals are client data; ask OpenRouter not to train on it.
        "provider": {"data_collection": "deny"},
    }
    try:
        data = _post_with_retry(body, api_key)
    except Exception as exc:  # fail safe: callers must not write on error
        return {
            "mode": "error",
            "model": MODEL,
            "correlation_id": correlation_id,
            "question_version": QUESTION_VERSION,
            "latency_ms": elapsed(),
            "error": f"{type(exc).__name__}: {exc}",
        }

    answers = data.get("answers", {})
    resolved_model = data.get("model", MODEL)

    rb = answers.get("risk_band", {})
    na = answers.get("next_action", {})
    nr = answers.get("needs_review", {})
    lp = answers.get("loss_probability", {})

    risk = _pick_score_level(rb, RISK_LEVELS)
    action = na.get("choice") or "human_review"
    review = float(nr.get("noul", 0.5))
    loss = float(lp.get("noul", 0.5))

    return {
        "mode": "live-openrouter",
        "model": resolved_model,
        "model_requested": MODEL,
        "correlation_id": correlation_id,
        "question_version": QUESTION_VERSION,
        "latency_ms": elapsed(),
        "answers": {
            "risk_band": {
                "value": risk,
                "confidence": rb.get("confidence", 0.5),
                "probabilities": {
                    RISK_LEVELS[int(k)]: v
                    for k, v in (rb.get("probabilities") or {}).items()
                    if str(k).isdigit() and int(k) < len(RISK_LEVELS)
                },
            },
            "next_action": {
                "value": action,
                "confidence": na.get("confidence", 0.5),
                "probabilities": dict(na.get("probabilities") or {}),
            },
            "needs_review": {"value": review},
            "loss_probability": {"value": loss},
        },
    }


def main():
    """Smoke test: decide one fixture deal through OpenRouter. Read-only."""
    import sys

    if not os.environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY is not set. Set it (local env only), then re-run.")
        sys.exit(2)

    from . import state

    fixture_path = os.path.join(
        os.path.dirname(__file__), "..", "fixtures", "deals.json"
    )
    with open(fixture_path) as f:
        deals = json.load(f)
    deal = deals[0] if isinstance(deals, list) else deals
    deal_state = state.build_state(deal)

    result = decide(deal_state)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result["mode"] == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
