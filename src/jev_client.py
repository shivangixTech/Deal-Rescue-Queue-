"""Jev decision client.

Three transports, one interface:
  - LIVE (typesafe): TypeSafe SDK (needs TYPESAFE_API_KEY). Real Jev judgments.
  - LIVE (openrouter): OpenRouter's typed /v1/systemone endpoint
    (needs OPENROUTER_API_KEY). The same real Jev judgments, billed
    per-call to the OpenRouter account (~$0.042/1M input tokens).
  - MOCK: deterministic rule-based stand-in used until a key arrives.

Every result carries "mode" so a mock is NEVER presented as a live Jev
judgment — in the API, the UI banner, and the logs. Failures return an error
result instead of raising, so callers can fail safe (no write-back).
"""

import os
import time
import uuid
from typing import Any, Dict

import requests

from .questions import NEXT_ACTIONS, QUESTION_VERSION, RISK_LEVELS

MODEL_ALIAS = os.environ.get("JEV_MODEL", "jev-1")

# --- mock rules (deterministic; clearly labeled, never live) -----------------

_NEG_STRONG = ("went with a competitor", "chose a competitor", "not moving forward", "cancelled")
# NOTE: "budget" and bare "legal" are deliberately NOT blockers — "budget approved"
# and "with legal for final signature" are positive. A blocker must be explicit.
_BLOCKER = ("security review", "redlines", "procurement", "infosec", "objection", "champion left", "discount")
_POSITIVE = ("approved", "verbal yes", "excited", "signed", "moving forward")


def _spread(n: int, winner: float = 0.72) -> Dict[str, float]:
    rest = round((1.0 - winner) / max(n - 1, 1), 4)
    return {"_winner": winner, "_rest": rest}


def _decide_mock(state: Dict[str, Any], correlation_id: str, t0: float) -> Dict[str, Any]:
    note = (state.get("latest_note") or "").lower()
    days = state.get("days_since_activity") or 0
    n_activities = len(state.get("activity_summary") or [])

    def probs(options, chosen):
        s = _spread(len(options))
        return {o: (s["_winner"] if o == chosen else s["_rest"]) for o in options}

    # Sparse context: no usable signal at all.
    if not note and n_activities <= 1:
        risk, action, review, conf, loss = "Watch", "human_review", 0.88, 0.45, 0.50
    # Explicit rejection: the call is clear, but closing is high-impact.
    elif any(k in note for k in _NEG_STRONG):
        risk, action, review, conf, loss = "Critical", "review_close_lost", 0.20, 0.85, 0.90
    else:
        level = 1  # Watch baseline
        has_blocker = any(k in note for k in _BLOCKER)
        has_positive = any(k in note for k in _POSITIVE)
        if has_blocker:
            level = max(level, 2)
        if days >= 12 and level < 2:
            level += 1
        if has_positive and not has_blocker:
            if days >= 10:
                # Contradiction: positive words, stale deal, and no blocker to
                # explain the silence. Cap risk, force human review.
                return _mock_result(state, correlation_id, t0, "Watch", "email_followup", 0.78, 0.50, 0.45,
                                    note="MOCK rule: positive note but stale activity")
            if days < 10:
                level = max(0, level - 1)
        risk = RISK_LEVELS[level]
        if risk in ("High", "Critical"):
            action = "call_now"
        elif risk == "Watch":
            action = "email_followup"
        else:
            action = "email_followup" if state.get("stage") == "Negotiation" else "nurture"
        if state.get("stage") in ("Discovery", "Qualification") and (state.get("amount") or 0) < 10000 and level <= 1:
            action = "nurture"
        review, conf = 0.20, 0.82
        loss = {"Critical": 0.85, "High": 0.60, "Watch": 0.30, "Healthy": 0.10}[risk]

    return _mock_result(state, correlation_id, t0, risk, action, review, conf, loss,
                        prob_fn=lambda opts, c: probs(opts, c))


def _mock_result(state, correlation_id, t0, risk, action, review, conf, loss, prob_fn=None, note=""):
    def probs(options, chosen):
        s = _spread(len(options))
        return {o: (s["_winner"] if o == chosen else s["_rest"]) for o in options}

    pf = prob_fn or probs
    return {
        "mode": "mock",
        "model": "mock-rules-v1",
        "mock_note": ("MOCK — deterministic stand-in, not a Jev judgment. " + note).strip(),
        "correlation_id": correlation_id,
        "question_version": QUESTION_VERSION,
        "latency_ms": int((time.time() - t0) * 1000),
        "answers": {
            "risk_band": {"value": risk, "confidence": conf, "probabilities": pf(RISK_LEVELS, risk)},
            "next_action": {"value": action, "confidence": conf, "probabilities": pf(list(NEXT_ACTIONS), action)},
            "needs_review": {"value": review},
            "loss_probability": {"value": loss},
        },
    }


# --- live transport ----------------------------------------------------------

# Jev's documented transient errors: 429 (rate limit) and 529 (overload).
# We never retry anything else (401/422 are permanent — retrying them is wrong).
# The SDK isn't needed to recognize these: we look at the exception's message.
_TRANSIENT_HINTS = ("429", "529", "rate_limit", "rate limit", "overloaded", "overload")
_RETRY_ATTEMPTS = 3          # first call + up to 2 retries
_RETRY_BASE_SECS = 1.0       # backoff doubles each time: 1s, 2s


def _is_transient(exc):
    msg = str(exc).lower()
    return any(hint in msg for hint in _TRANSIENT_HINTS)


def _call_with_retry(fn):
    """Run fn(); retry once or twice on transient Jev errors, then give up."""
    last = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if not _is_transient(exc) or attempt == _RETRY_ATTEMPTS - 1:
                raise
            time.sleep(_RETRY_BASE_SECS * (2 ** attempt))
    raise last  # unreachable, but keeps the type checker honest


def _to_sdk_questions(spec: Dict[str, Any]):
    from typesafe_sdk import Choice, Noul, Score

    out = {}
    for name, q in spec.items():
        kind = q["kind"]
        if kind == "score":
            out[name] = Score(instructions=q["instructions"], criteria=q["criteria"])
        elif kind == "choice":
            out[name] = Choice(instructions=q["instructions"], criteria=q["criteria"])
        elif kind == "noul":
            out[name] = Noul(instructions=q["instructions"])
    return out


def _decide_live(state: Dict[str, Any], spec: Dict[str, Any], correlation_id: str, t0: float) -> Dict[str, Any]:
    from typesafe_sdk import TypeSafeClient

    with TypeSafeClient() as client:
        response = _call_with_retry(
            lambda: client.system_one(state=state, questions=_to_sdk_questions(spec), model=MODEL_ALIAS)
        )
    rb, na, nr = response.scores["risk_band"], response.choices["next_action"], response.nouls["needs_review"]
    lp = response.nouls["loss_probability"]
    return {
        "mode": "live",
        "model": MODEL_ALIAS,
        "correlation_id": correlation_id,
        "question_version": QUESTION_VERSION,
        "latency_ms": int((time.time() - t0) * 1000),
        "answers": {
            "risk_band": {"value": rb.score, "confidence": rb.confidence, "probabilities": dict(rb.probabilities)},
            "next_action": {"value": na.choice, "confidence": na.confidence, "probabilities": dict(na.probabilities)},
            "needs_review": {"value": nr.noul},
            "loss_probability": {"value": lp.noul},
        },
    }


# --- OpenRouter transport (verified typed endpoint) --------------------------
# POST https://openrouter.ai/api/v1/systemone serves the real Jev System One
# API (typed Choice/Score/Noul with probabilities and confidence), billed to
# the OpenRouter account. Verified request/response shape against the live
# endpoint docs; the two gotchas below are real 400s.
#
# Cost: ~$0.042 per 1M input tokens, output free — one decision ≈ $0.00003.

OPENROUTER_URL = "https://openrouter.ai/api/v1/systemone"
OPENROUTER_MODEL = os.environ.get("OPENROUTER_JEV_MODEL", "typesafe/jev-1.13")


def _to_openrouter_questions(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Map our spec to the endpoint's shape. Choice.criteria must be an OBJECT
    and Score.criteria an ARRAY of {label, description} — swapping them is a
    400 ("expected record, received array" and vice versa)."""
    out = {}
    for name, q in spec.items():
        kind = q["kind"]
        if kind == "score":
            out[name] = {
                "type": "score",
                "instructions": q["instructions"],
                "criteria": [{"label": lvl, "description": lvl} for lvl in q["criteria"]],
            }
        elif kind == "choice":
            out[name] = {"type": "choice", "instructions": q["instructions"], "criteria": q["criteria"]}
        elif kind == "noul":
            out[name] = {"type": "noul", "instructions": q["instructions"]}
    return out


def _decide_openrouter(state: Dict[str, Any], spec: Dict[str, Any],
                       correlation_id: str, t0: float) -> Dict[str, Any]:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    payload = {"model": OPENROUTER_MODEL, "state": state, "questions": _to_openrouter_questions(spec)}

    def do():
        resp = requests.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if resp.status_code != 200:
            # 401 = bad key, 402 = out of credits (never retry these).
            raise RuntimeError(f"openrouter HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    data = _call_with_retry(do)
    answers = data.get("answers") or {}
    rb, na = answers["risk_band"], answers["next_action"]
    nr, lp = answers["needs_review"], answers["loss_probability"]
    levels = RISK_LEVELS
    idx = max(0, min(len(levels) - 1, int(round(float(rb["score"])))))
    rb_probs = {}
    for k, v in (rb.get("probabilities") or {}).items():
        if str(k).isdigit() and int(k) < len(levels):
            rb_probs[levels[int(k)]] = v
    return {
        "mode": "live",
        "transport": "openrouter",
        "model": data.get("model") or OPENROUTER_MODEL,
        "correlation_id": correlation_id,
        "question_version": QUESTION_VERSION,
        "latency_ms": int((time.time() - t0) * 1000),
        "answers": {
            "risk_band": {"value": levels[idx], "confidence": rb.get("confidence"), "probabilities": rb_probs},
            "next_action": {"value": na["choice"], "confidence": na.get("confidence"),
                            "probabilities": dict(na.get("probabilities") or {})},
            "needs_review": {"value": nr["noul"]},
            "loss_probability": {"value": lp["noul"]},
        },
    }


# --- public entry point ------------------------------------------------------


def decide(state: Dict[str, Any], spec: Dict[str, Any] = None) -> Dict[str, Any]:
    """Run the three Jev questions against one deal state. Never raises."""
    from .questions import question_spec

    spec = spec or question_spec()
    correlation_id = uuid.uuid4().hex[:12]
    t0 = time.time()
    try:
        if os.environ.get("TYPESAFE_API_KEY"):
            return _decide_live(state, spec, correlation_id, t0)
        if os.environ.get("OPENROUTER_API_KEY"):
            return _decide_openrouter(state, spec, correlation_id, t0)
        return _decide_mock(state, correlation_id, t0)
    except Exception as exc:  # fail safe: callers must not write on error
        return {
            "mode": "error",
            "model": MODEL_ALIAS,
            "correlation_id": correlation_id,
            "question_version": QUESTION_VERSION,
            "latency_ms": int((time.time() - t0) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }
