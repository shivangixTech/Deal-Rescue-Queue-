"""Build the canonical deal state object that is sent to Jev.

The state is the ONLY thing Jev sees. Keep it minimal, typed, and explicit:
missing values are None (never invented), notes are trimmed, and only the
newest activities are included.
"""

from typing import Any, Dict, List

REQUIRED_FIELDS = ("deal_id", "stage", "amount")
STATE_VERSION = "v1"
MAX_ACTIVITIES = 5
MAX_NOTE_CHARS = 1000


def build_state(deal: Dict[str, Any]) -> Dict[str, Any]:
    activities = deal.get("activities") or []
    summary: List[Dict[str, Any]] = [
        {
            "type": a.get("type"),
            "text": (a.get("text") or "")[:300],
            "days_ago": a.get("days_ago"),
        }
        for a in activities[:MAX_ACTIVITIES]
    ]
    note = deal.get("latest_note")
    return {
        "deal_id": deal.get("deal_id"),
        "deal_name": deal.get("deal_name"),
        "company": deal.get("company"),
        "stage": deal.get("stage"),
        "amount": deal.get("amount"),
        "close_date": deal.get("close_date"),
        "days_since_activity": deal.get("days_since_activity"),
        "latest_note": note.strip()[:MAX_NOTE_CHARS] if isinstance(note, str) and note.strip() else None,
        "activity_summary": summary,
        "state_version": STATE_VERSION,
    }


def missing_required_fields(state: Dict[str, Any]) -> List[str]:
    """Fields without which no automated recommendation is safe."""
    return [f for f in REQUIRED_FIELDS if state.get(f) in (None, "")]
