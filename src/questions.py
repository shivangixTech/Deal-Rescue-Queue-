"""Jev question definitions for the Deal Rescue Queue.

Design rule: atomic questions. Diagnosis (risk), recommendation (next action),
and uncertainty (review need) are asked independently; ordinary code in
policy.py combines them. Bump QUESTION_VERSION whenever these change so every
logged decision stays traceable.
"""

from typing import Dict

QUESTION_VERSION = "v2"

RISK_LEVELS = ["Healthy", "Watch", "High", "Critical"]

NEXT_ACTIONS = {
    "call_now": "Call the stakeholder now; the deal needs direct human intervention.",
    "email_followup": "Send a written follow-up or reminder; no urgent call is needed.",
    "nurture": "No urgent action; keep the deal warm with light touches.",
    "review_close_lost": "Evidence suggests the deal is lost; a human must review before it is closed.",
    "human_review": "The context is unclear; a human should decide the next step.",
}


def question_spec() -> Dict:
    """Plain-dict spec. jev_client converts it to SDK objects for live calls
    and consumes it directly for the mock path."""
    return {
        "risk_band": {
            "kind": "score",
            "instructions": (
                "Rate this deal's risk of stalling or being lost, using ONLY the "
                "provided state. Levels: Healthy = active progress and no material "
                "blocker; Watch = mild delay or incomplete context; High = clear "
                "blocker, prolonged inactivity, or lost champion; Critical = imminent "
                "loss, explicit rejection, or severe contradiction."
            ),
            "criteria": RISK_LEVELS,
        },
        "next_action": {
            "kind": "choice",
            "instructions": (
                "Choose the single best next step for the sales rep, given the "
                "deal state. Prefer the least invasive action that still addresses "
                "the risk."
            ),
            "criteria": NEXT_ACTIONS,
        },
        "needs_review": {
            "kind": "noul",
            "instructions": (
                "The available context is missing, contradictory, or insufficient "
                "to decide safely. Answer yes when a human should review this deal "
                "before any action is taken or recommended."
            ),
        },
        "loss_probability": {
            "kind": "noul",
            "instructions": (
                "If no action is taken in the next 14 days, this deal will stall "
                "or be lost. Consider staleness, blockers, stage, and engagement "
                "trend — not just the latest note."
            ),
        },
    }
