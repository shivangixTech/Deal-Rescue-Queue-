"""Milestone 3: guarded write-back of Jev judgments to HubSpot.

A real CRM write only ever happens through this module, and only when a
human explicitly approves it:

  1. POST /api/writeback/{deal_id} with dry_run=true (the default)
     -> returns a preview of the four drq_* properties that WOULD be
        written. Nothing touches HubSpot.
  2. Same call with {"approved": true, "dry_run": false} and ?source=live
     -> real PATCH through the Ampersand proxy onto the HubSpot deal.

Prerequisite: create four custom deal properties in HubSpot
(Settings -> Properties -> Deal properties) with these EXACT internal names:
  drq_risk_band, drq_recommended_action, drq_reviewed_at, drq_reviewer
If any property is missing, HubSpot answers HTTP 400 and the error text is
returned verbatim, so the missing field is obvious.

The fixtures source is preview-only: real writes need ?source=live, because
only live deals carry a HubSpot id to write to. Every preview and every
real write is appended to logs/decisions.jsonl for audit.
"""

import requests
from fastapi import HTTPException, Query
from pydantic import BaseModel

from .ampersand_client import PROXY_BASE, TIMEOUT_S, AmpersandError, _creds

DRQ_FIELDS = ("drq_risk_band", "drq_recommended_action",
              "drq_reviewed_at", "drq_reviewer")

WRITEBACK_VERSION = "m3-v1"


class WritebackError(RuntimeError):
    """Raised when a write-back cannot be performed safely."""


def _hubspot_id(deal_id):
    """'HS-12345' -> '12345'. Only live deals have a HubSpot id."""
    if isinstance(deal_id, str) and deal_id.startswith("HS-") and deal_id[3:].isdigit():
        return deal_id[3:]
    raise WritebackError(
        "deal_id %r is not a live HubSpot deal (expected 'HS-<id>')" % (deal_id,))


def build_payload(answers, reviewer):
    """Turn Jev's typed answers into the four drq_* HubSpot properties."""
    answers = answers or {}
    risk = (answers.get("risk_band") or {}).get("value")
    action = (answers.get("next_action") or {}).get("value")
    if not risk or not action:
        raise WritebackError("no Jev judgment to write back (missing risk_band/next_action)")
    from datetime import datetime, timezone
    return {
        "drq_risk_band": risk,
        "drq_recommended_action": action,
        "drq_reviewed_at": datetime.now(timezone.utc).isoformat(),
        "drq_reviewer": reviewer or "demo-reviewer",
    }


def _proxy_headers():
    key, project, installation = _creds()
    return {
        "x-amp-proxy-version": "1",
        "x-amp-project-id": project,
        "x-api-key": key,
        "x-amp-installation-id": installation,
        "Content-Type": "application/json",
    }


def apply_writeback(deal_id, payload, approved):
    """PATCH the four drq_* properties onto the HubSpot deal via the proxy.

    approved must be True (the human's click in the UI). This check is the
    belt next to the endpoint's own checks; calling with approved=False
    always raises, never writes.
    """
    if not approved:
        raise WritebackError("refusing to write: human approval is required")
    hs_id = _hubspot_id(deal_id)
    props = {k: v for k, v in (payload or {}).items()
             if k in DRQ_FIELDS and v is not None}
    missing = [f for f in DRQ_FIELDS if f not in props]
    if missing:
        raise WritebackError("nothing to write: missing values for " + ", ".join(missing))
    try:
        resp = requests.patch(
            "%s/crm/v3/objects/deals/%s" % (PROXY_BASE, hs_id),
            json={"properties": props},
            headers=_proxy_headers(),
            timeout=TIMEOUT_S,
        )
    except requests.RequestException as e:
        raise WritebackError("proxy request failed: %s" % (e,))
    if resp.status_code not in (200, 201):
        raise WritebackError(
            "HubSpot rejected the write (HTTP %s): %s" % (resp.status_code, resp.text[:400]))
    try:
        return resp.json()
    except ValueError:
        return {"ok": True, "raw": resp.text[:400]}


class WritebackIn(BaseModel):
    approved: bool = False
    reviewer: str = "demo-reviewer"
    dry_run: bool = True


def attach(app, decide_deal, load_deals, log_fn):
    """Mount the write-back endpoint on the existing FastAPI app.

    decide_deal / load_deals / log_fn are passed in (not imported) so this
    module never creates a circular import with app.py.
    """

    @app.post("/api/writeback/{deal_id}")
    def writeback(deal_id: str, body: WritebackIn, source: str = Query("fixtures")):
        live = (source or "fixtures").lower() == "live"
        src = "live" if live else "fixtures"
        try:
            deals = {d["deal_id"]: d for d in load_deals(src)}
        except AmpersandError as e:
            raise HTTPException(502, "live HubSpot fetch failed: %s" % (e,))
        if deal_id not in deals:
            raise HTTPException(404, "unknown deal_id")
        # Recompute the decision server-side: the client never supplies
        # field values, so nothing unapproved can be injected.
        item = decide_deal(deals[deal_id])
        answers = (item.get("decision") or {}).get("answers") or {}
        if "risk_band" not in answers or "next_action" not in answers:
            raise HTTPException(502, "no Jev judgment available for this deal")
        try:
            payload = build_payload(answers, body.reviewer)
        except WritebackError as e:
            raise HTTPException(502, "write-back failed: %s" % (e,))

        if body.dry_run or not body.approved:
            preview = {
                "deal_id": deal_id,
                "source": "hubspot-live" if live else "fixtures",
                "dry_run": True,
                "approved": body.approved,
                "reviewer": body.reviewer,
                "would_write": payload if body.approved else {},
                "writeback_version": WRITEBACK_VERSION,
                "note": ("Preview only: nothing was written. Send approved=true "
                         "with dry_run=false on ?source=live to write."),
            }
            log_fn({"deal_id": deal_id, "writeback_preview": preview})
            return preview

        # Real write: approved=True, dry_run=False, live source required.
        if not live:
            raise HTTPException(
                400, "real writes need ?source=live (fixtures have no HubSpot id)")

        # Idempotency: never write twice. If drq_reviewed_at is already set,
        # a previous approval already wrote these fields — report it instead
        # of PATCHing again. The check is best-effort (a failed read does not
        # block a legitimate write); the write itself stays fail-closed.
        try:
            chk = requests.get(
                "%s/crm/v3/objects/deals/%s" % (PROXY_BASE, _hubspot_id(deal_id)),
                params={"properties": "drq_reviewed_at,drq_reviewer"},
                headers=_proxy_headers(),
                timeout=TIMEOUT_S,
            )
            if chk.status_code == 200:
                ex = chk.json().get("properties") or {}
                if ex.get("drq_reviewed_at"):
                    result = {
                        "deal_id": deal_id,
                        "source": "hubspot-live",
                        "dry_run": False,
                        "approved": True,
                        "written": False,
                        "already_written": True,
                        "existing_reviewed_at": ex.get("drq_reviewed_at"),
                        "existing_reviewer": ex.get("drq_reviewer"),
                        "writeback_version": WRITEBACK_VERSION,
                        "note": "Skipped: drq_reviewed_at already set — no duplicate write.",
                    }
                    log_fn({"deal_id": deal_id, "writeback_skipped": result})
                    return result
        except requests.RequestException:
            pass

        try:
            hs = apply_writeback(deal_id, payload, approved=True)
        except WritebackError as e:
            raise HTTPException(502, "write-back failed: %s" % (e,))
        result = {
            "deal_id": deal_id,
            "source": "hubspot-live",
            "dry_run": False,
            "approved": True,
            "reviewer": body.reviewer,
            "wrote": payload,
            "hubspot": hs,
            "writeback_version": WRITEBACK_VERSION,
        }
        log_fn({"deal_id": deal_id, "writeback": result, "real_write": True})
        return result

    return app
