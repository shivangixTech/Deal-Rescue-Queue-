"""Decision contract: turn Jev answers + deal state into a route.

Jev never owns side effects. This module does. Rules, in order:

1. Missing required context        -> human_review (never decide blind)
2. needs_review >= 0.65            -> human_review (model-flagged uncertainty)
3. Any confidence < 0.60           -> human_review (uncertain judgment)
4. High-impact action requested    -> human_review (close-lost needs sign-off)
5. Otherwise                       -> recommendation_ready

Thresholds are starting hypotheses, tuned against tests/decision-cases.json.
"""

from typing import Any, Dict, List

from .questions import RISK_LEVELS
from .state import missing_required_fields

POLICY_VERSION = "v1"
NEEDS_REVIEW_THRESHOLD = 0.65
MIN_CONFIDENCE = 0.60
# Actions that must never fire without a human: closing a deal is irreversible.
HIGH_IMPACT_ACTIONS = {"review_close_lost"}

RISK_ORDER = {level: i for i, level in enumerate(RISK_LEVELS)}


def value_at_risk(state: Dict[str, Any], decision: Dict[str, Any]) -> float:
    """Expected business value at stake: amount x P(loss in 14 days).

    A deal that is already lost (review_close_lost) has nothing left to save,
    so its savable value is 0 — it sinks to the bottom of the queue. This is
    what turns a plain risk list into a prioritization: the top of the queue
    is where action saves the most money.
    """
    answers = decision.get("answers") or {}
    loss_p = (answers.get("loss_probability") or {}).get("value") or 0.0
    amount = state.get("amount") or 0
    action = (answers.get("next_action") or {}).get("value")
    savable = 0.0 if action == "review_close_lost" else 1.0
    return round(amount * loss_p * savable, 2)


def sort_key(item: Dict[str, Any]) -> float:
    """Highest savable value first. This is the queue's real ordering."""
    return -item["value_at_risk"]


def route(state: Dict[str, Any], decision: Dict[str, Any]) -> Dict[str, Any]:
    reasons: List[str] = []
    if decision.get("mode") == "error":
        return {"route": "error", "reasons": [f"decision failed: {decision.get('error')}"], "policy_version": POLICY_VERSION}

    missing = missing_required_fields(state)
    if missing:
        return {"route": "human_review", "reasons": [f"missing required context: {', '.join(missing)}"],
                "policy_version": POLICY_VERSION}

    answers = decision["answers"]
    review_p = answers["needs_review"]["value"]
    if review_p >= NEEDS_REVIEW_THRESHOLD:
        reasons.append(f"needs_review={review_p:.2f} >= {NEEDS_REVIEW_THRESHOLD}")
        return {"route": "human_review", "reasons": reasons, "policy_version": POLICY_VERSION}

    risk_conf = answers["risk_band"]["confidence"]
    action_conf = answers["next_action"]["confidence"]
    if min(risk_conf, action_conf) < MIN_CONFIDENCE:
        reasons.append(f"low confidence (risk={risk_conf:.2f}, action={action_conf:.2f}) < {MIN_CONFIDENCE}")
        return {"route": "human_review", "reasons": reasons, "policy_version": POLICY_VERSION}

    action = answers["next_action"]["value"]
    if action in HIGH_IMPACT_ACTIONS:
        reasons.append(f"high-impact action '{action}' requires human sign-off")
        return {"route": "human_review", "reasons": reasons, "policy_version": POLICY_VERSION}

    reasons.append("confidence sufficient; no high-impact action")
    return {"route": "recommendation_ready", "reasons": reasons, "policy_version": POLICY_VERSION}

