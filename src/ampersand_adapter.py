"""HubSpot -> Deal Rescue Queue adapter (Milestone 2, Step 8).

Reads live deals from HubSpot through the Ampersand proxy and converts them
into the plain deal-dict shape that build_state() expects. Fixtures stay as
the fallback; this module WRITES NOTHING to HubSpot.

Required environment variables (Step 5 / Step 6 values):
    AMP_API_KEY          Ampersand API key of type "Server or CLI"
    AMP_PROJECT_ID       Ampersand project ID (Project settings page)
    AMP_INSTALLATION_ID  Installation ID from the install flow (Step 6)

Quick check (Windows cmd):
    set AMP_API_KEY=... & set AMP_PROJECT_ID=... & set AMP_INSTALLATION_ID=...
    python -m src.ampersand_adapter
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PROXY_BASE = "https://proxy.withampersand.com"

# HubSpot pipeline stages are internal IDs, not display names. These are the
# DEFAULT sales pipeline IDs. Verify yours: HubSpot -> Settings -> Objects ->
# Deals -> Pipelines -> click a stage -> "Internal value". Any ID not listed
# here passes through unchanged (with a stderr warning) so no deal is dropped.
DEALSTAGE_TO_LABEL = {
    "appointmentscheduled": "Discovery",
    "qualifiedtobuy": "Qualification",
    "presentationscheduled": "Proposal",
    "decisionmakerboughtin": "Negotiation",
    "contractsent": "Negotiation",
    "closedwon": "Closed Won",
    "closedlost": "Closed Lost",
}

DEAL_PROPERTIES = "dealname,amount,dealstage,closedate,notes_last_updated"
NOTE_CHARS = 1000


def _headers():
    missing = [n for n in ("AMP_API_KEY", "AMP_PROJECT_ID", "AMP_INSTALLATION_ID")
               if not os.environ.get(n)]
    if missing:
        raise RuntimeError("Set these environment variables first: " + ", ".join(missing))
    return {
        "x-amp-proxy-version": "1",
        "x-amp-project-id": os.environ["AMP_PROJECT_ID"],
        "x-api-key": os.environ["AMP_API_KEY"],
        "x-amp-installation-id": os.environ["AMP_INSTALLATION_ID"],
        "Accept": "application/json",
    }


def _warn(msg):
    print("warning: " + msg, file=sys.stderr)


def _proxy_get(path, params=None):
    url = PROXY_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"proxy {e.code} on {path}: {body}") from e


def _to_float(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _to_date(value):
    """HubSpot date property: 'YYYY-MM-DD' string, or a millisecond timestamp."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    return str(value)[:10]


def _days_since(ms):
    try:
        ts = int(ms) / 1000
    except (TypeError, ValueError):
        return None
    return max(0, int((datetime.now(timezone.utc).timestamp() - ts) / 86400))


def fetch_deals(limit=100, include_details=True):
    """Return HubSpot deals as build_state()-ready dicts."""
    data = _proxy_get("/crm/v3/objects/deals",
                      {"limit": limit, "properties": DEAL_PROPERTIES})
    return [_to_deal(hs, include_details=include_details)
            for hs in data.get("results", [])]


def _to_deal(hs, include_details=True):
    props = hs.get("properties", {}) or {}
    hs_id = hs.get("id")
    stage_id = str(props.get("dealstage") or "").strip().lower()
    if stage_id and stage_id not in DEALSTAGE_TO_LABEL:
        _warn(f"unmapped dealstage '{props.get('dealstage')}' on deal {hs_id}; passing through")
    deal = {
        "deal_id": f"hubspot-{hs_id}",
        "deal_name": props.get("dealname"),
        "company": None,      # filled below when possible
        "stage": DEALSTAGE_TO_LABEL.get(stage_id, props.get("dealstage")),
        "amount": _to_float(props.get("amount")),
        "close_date": _to_date(props.get("closedate")),
        "days_since_activity": _days_since(props.get("notes_last_updated")),
        "latest_note": None,  # filled below when possible
        "activities": [],
    }
    if include_details:
        deal["company"] = _fetch_company_name(hs_id)
        deal["latest_note"] = _fetch_latest_note(hs_id)
    return deal


def _first_associated_id(deal_id, to_object):
    data = _proxy_get(f"/crm/v3/objects/deals/{deal_id}/associations/{to_object}",
                      {"limit": 1})
    results = data.get("results", [])
    return results[0]["id"] if results else None


def _fetch_company_name(deal_id):
    try:
        company_id = _first_associated_id(deal_id, "companies")
        if not company_id:
            return None
        data = _proxy_get(f"/crm/v3/objects/companies/{company_id}",
                          {"properties": "name"})
        return (data.get("properties", {}) or {}).get("name")
    except Exception as e:  # nice-to-have; never drop the deal
        _warn(f"company lookup failed for deal {deal_id}: {e}")
        return None


def _fetch_latest_note(deal_id):
    try:
        data = _proxy_get(f"/crm/v3/objects/deals/{deal_id}/associations/notes",
                          {"limit": 10})
        note_ids = [r["id"] for r in data.get("results", [])]
        if not note_ids:
            return None
        req = urllib.request.Request(
            PROXY_BASE + "/crm/v3/objects/notes/batch/read",
            data=json.dumps({
                "inputs": [{"id": i} for i in note_ids],
                "properties": ["hs_note_body", "hs_createdate"],
            }).encode(),
            headers={**_headers(), "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            notes = json.load(resp).get("results", [])
        bodies = []
        for n in notes:
            p = n.get("properties", {}) or {}
            body = (p.get("hs_note_body") or "").strip()
            if body:
                bodies.append((p.get("hs_createdate") or "", body))
        if not bodies:
            return None
        bodies.sort(reverse=True)  # newest first
        return bodies[0][1][:NOTE_CHARS]
    except Exception as e:  # nice-to-have; never drop the deal
        _warn(f"note lookup failed for deal {deal_id}: {e}")
        return None


def main():
    try:
        deals = fetch_deals()
    except RuntimeError as e:
        print("error: " + str(e), file=sys.stderr)
        raise SystemExit(1)
    print(f"{len(deals)} deals from HubSpot (via Ampersand proxy)")
    print(f"{'deal_id':<18}{'stage':<14}{'amount':>12}  name")
    for d in deals:
        amount = f"${d['amount']:,.0f}" if d["amount"] else "-"
        print(f"{d['deal_id']:<18}{str(d['stage']):<14}{amount:>12}  {d['deal_name']}")
    print("\nThese dicts feed build_state() directly; notes/companies included where found.")


if __name__ == "__main__":
    main()
