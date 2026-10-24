"""Deal Rescue Queue API (Milestones 1-2).

Serves the review queue UI and the decision endpoints.
- M1: fixtures + Jev client (mock or live) + policy.
- M2: ?source=live reads HubSpot deals through the Ampersand proxy
  (src/ampersand_client.py). The default remains fixtures.
- /review only returns a dry-run preview of what WOULD be written.
Every decision is appended to logs/decisions.jsonl for audit.
"""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import ampersand_client, jev_client, policy, writeback
from .questions import QUESTION_VERSION, question_spec
from .state import build_state

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
LOG_DIR = os.path.join(ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

app = FastAPI(title="Deal Rescue Queue (M2)")
SPEC = question_spec()


_log_lock = threading.Lock()


def _log(entry: dict) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(), **entry}
    with _log_lock:
        with open(os.path.join(LOG_DIR, "decisions.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def _resolve_source(source: str) -> str:
    s = (source or "fixtures").lower()
    if s not in ("fixtures", "live"):
        raise HTTPException(400, "source must be 'fixtures' or 'live'")
    return s


def _load_deals(source: str = "fixtures"):
    if source == "live":
        return ampersand_client.fetch_live_deals()
    with open(os.path.join(ROOT, "fixtures", "deals.json"), encoding="utf-8") as f:
        return json.load(f)


def _ampersand_configured() -> bool:
    return all(os.environ.get(v, "").strip() for v in
               ("AMPERSAND_API_KEY", "AMPERSAND_PROJECT_ID", "AMPERSAND_INSTALLATION_ID"))


def decide_deal(deal: dict) -> dict:
    state = build_state(deal)
    decision = jev_client.decide(state, SPEC)
    result = policy.route(state, decision)
    var = policy.value_at_risk(state, decision)
    _log({"deal_id": state.get("deal_id"), "mode": decision.get("mode"),
          "model": decision.get("model"), "correlation_id": decision.get("correlation_id"),
          "question_version": decision.get("question_version"),
          "policy_version": result.get("policy_version"),
          "route": result.get("route"), "reasons": result.get("reasons"),
          "value_at_risk": var,
          "answers": decision.get("answers"), "error": decision.get("error")})
    return {"deal": {"deal_id": deal.get("deal_id"), "deal_name": deal.get("deal_name"),
                     "company": deal.get("company"), "stage": deal.get("stage"),
                     "amount": deal.get("amount"), "close_date": deal.get("close_date"),
                     "days_since_activity": deal.get("days_since_activity")},
            "decision": decision, "route": result, "value_at_risk": var}


class ReviewIn(BaseModel):
    approved: bool
    reviewer: str = "demo-reviewer"


@app.get("/")
def index():
    return FileResponse(os.path.join(ROOT, "src", "web", "index.html"))


@app.get("/api/health")
def health():
    return {"ok": True, "mode": "live" if os.environ.get("TYPESAFE_API_KEY") else "mock",
            "ampersand_configured": _ampersand_configured(),
            "question_version": QUESTION_VERSION, "policy_version": policy.POLICY_VERSION}


@app.get("/api/queue")
def queue(source: str = Query("fixtures")):
    src = _resolve_source(source)
    t0 = time.time()
    try:
        deals = _load_deals(src)
    except ampersand_client.AmpersandError as e:
        raise HTTPException(502, f"live HubSpot fetch failed: {e}")
    # Decide all deals in parallel: with a live Jev transport each decide_deal
    # makes 4 sequential HTTP calls, so 11 deals x 4 = 44 calls would take
    # ~2 min one-by-one. Threads cut that to roughly one deal's worth.
    # ex.map preserves input order, so ranking stays deterministic.
    with ThreadPoolExecutor(max_workers=min(12, len(deals) or 1)) as ex:
        items = list(ex.map(decide_deal, deals))
    items.sort(key=policy.sort_key)
    review_n = sum(1 for i in items if i["route"]["route"] == "human_review")
    return {"items": items, "count": len(items), "human_review_count": review_n,
            "source": "hubspot-live" if src == "live" else "fixtures",
            "latency_ms": int((time.time() - t0) * 1000)}


@app.post("/api/decide")
def decide_one(deal: dict):
    if not deal.get("deal_id"):
        raise HTTPException(400, "deal.deal_id is required")
    return decide_deal(deal)


@app.post("/api/review/{deal_id}")
def review(deal_id: str, body: ReviewIn, source: str = Query("fixtures")):
    """Dry-run write-back preview. M2 writes NOTHING to any CRM; M3 will gate
    the real write behind this same approval step."""
    src = _resolve_source(source)
    try:
        deals = {d["deal_id"]: d for d in _load_deals(src)}
    except ampersand_client.AmpersandError as e:
        raise HTTPException(502, f"live HubSpot fetch failed: {e}")
    if deal_id not in deals:
        raise HTTPException(404, "unknown deal_id")
    item = decide_deal(deals[deal_id])
    answers = item["decision"]["answers"]
    payload = {
        "deal_id": deal_id,
        "source": "hubspot-live" if src == "live" else "fixtures",
        "dry_run": True,
        "approved": body.approved,
        "reviewer": body.reviewer,
        "would_write": {
            "drq_risk_band": answers["risk_band"]["value"],
            "drq_recommended_action": answers["next_action"]["value"],
            "drq_reviewed_at": datetime.now(timezone.utc).isoformat(),
            "drq_reviewer": body.reviewer,
        } if body.approved else {},
        "note": "M2 dry-run only: nothing was written anywhere.",
    }
    _log({"deal_id": deal_id, "review": payload})
    return payload


# M3: guarded write-back. The endpoint is defined in writeback.py and mounted
# here so app.py keeps owning decide_deal/_load_deals/_log (no circular import).
writeback.attach(app, decide_deal=decide_deal, load_deals=_load_deals, log_fn=_log)
