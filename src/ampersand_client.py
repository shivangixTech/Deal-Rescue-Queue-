"""Milestone 2: read HubSpot deals through the Ampersand proxy.

Uses Proxy Actions (no webhooks or destinations needed): the FastAPI backend
calls proxy.withampersand.com with x-amp-* headers, and Ampersand forwards
the call to the customer's HubSpot using the stored OAuth credentials.

Required env (see .env.example):
  AMPERSAND_API_KEY         "Server or CLI" key from the Ampersand dashboard
  AMPERSAND_PROJECT_ID      project ID (dashboard project settings)
  AMPERSAND_INSTALLATION_ID installation ID (Integrations -> deal-rescue-hubspot)

Any failure raises AmpersandError; the API layer turns it into an HTTP 502.
Fixtures remain the default data source; live is opt-in via ?source=live.
"""

import os
from datetime import datetime, timezone

import requests

PROXY_BASE = "https://proxy.withampersand.com"
TIMEOUT_S = 20

# HubSpot dealstage internal value -> our canonical stage vocabulary.
STAGE_MAP = {
    "appointmentscheduled": "Discovery",
    "qualifiedtobuy": "Qualification",
    "presentationscheduled": "Proposal",
    "decisionmakerboughtin": "Negotiation",
    "contractsent": "Negotiation",
    "closedwon": "Closed Won",
    "closedlost": "Closed Lost",
}

PROPERTIES = ("dealname,amount,closedate,dealstage,description,pipeline,"
              "hs_lastmodifieddate,hs_createdate")


class AmpersandError(RuntimeError):
    """Raised for missing config or any proxy/HubSpot failure."""


def _creds():
    key = os.environ.get("AMPERSAND_API_KEY", "").strip()
    project = os.environ.get("AMPERSAND_PROJECT_ID", "").strip()
    installation = os.environ.get("AMPERSAND_INSTALLATION_ID", "").strip()
    missing = [n for n, v in (("AMPERSAND_API_KEY", key),
                              ("AMPERSAND_PROJECT_ID", project),
                              ("AMPERSAND_INSTALLATION_ID", installation)) if not v]
    if missing:
        raise AmpersandError("missing env: " + ", ".join(missing) + " (see .env.example)")
    return key, project, installation


def _parse_dt(value):
    """Parse a HubSpot datetime: epoch millis OR ISO 8601.

    HubSpot returns hs_lastmodifieddate/hs_createdate as ISO
    ('2026-10-12T18:51:41.635Z') but closedate as epoch millis.
    Returns an aware datetime, or None if unparseable.
    """
    if value in (None, ""):
        return None
    s = str(value).strip()
    if s.isdigit():
        try:
            return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc)
        except (ValueError, OSError):
            return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _to_date(value):
    """Normalize a HubSpot date to 'YYYY-MM-DD' (or None)."""
    dt = _parse_dt(value)
    return dt.date().isoformat() if dt else None


def _days_since(value):
    dt = _parse_dt(value)
    if not dt:
        return None
    return max(0, (datetime.now(timezone.utc) - dt).days)


def hubspot_to_canonical(hs):
    """Convert one HubSpot CRM v3 deal into our canonical fixture shape."""
    props = hs.get("properties", {}) or {}
    name = (props.get("dealname") or "").strip()
    company = name.split("—")[0].strip() if "—" in name else (name or None)
    stage_raw = (props.get("dealstage") or "").strip().lower()
    try:
        amount = float(props.get("amount")) if props.get("amount") not in (None, "") else None
    except (TypeError, ValueError):
        amount = None
    note = (props.get("description") or "").strip() or None
    # Prefer last-modified; fall back to created date. If HubSpot returns
    # neither, days_since_activity stays None and the UI shows "idle ?d".
    idle_days = _days_since(
        props.get("hs_lastmodifieddate") or props.get("hs_createdate"))
    return {
        "deal_id": f"HS-{hs.get('id')}",
        "deal_name": name or None,
        "company": company,
        "stage": STAGE_MAP.get(stage_raw, props.get("dealstage") or None),
        "amount": amount,
        "close_date": _to_date(props.get("closedate")),
        "days_since_activity": idle_days,
        "latest_note": note,
        "activities": [],
        "source": "hubspot-live",
    }


def fetch_live_deals():
    """Fetch deals via the Ampersand proxy; return canonical deal dicts."""
    key, project, installation = _creds()
    try:
        resp = requests.get(
            f"{PROXY_BASE}/crm/v3/objects/deals",
            params={"limit": 100, "properties": PROPERTIES},
            headers={
                "x-amp-proxy-version": "1",
                "x-amp-project-id": project,
                "x-api-key": key,
                "x-amp-installation-id": installation,
            },
            timeout=TIMEOUT_S,
        )
    except requests.RequestException as e:
        raise AmpersandError(f"proxy request failed: {e}")
    if resp.status_code != 200:
        raise AmpersandError(f"proxy returned HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        results = resp.json().get("results", [])
    except ValueError:
        raise AmpersandError("proxy returned a non-JSON response")
    if results:
        # One-line diagnostic: shows exactly which properties HubSpot returned
        # for the first deal (visible in the uvicorn terminal).
        fp = results[0].get("properties", {}) or {}
        print("[ampersand] first-deal property keys:",
              ",".join(sorted(fp.keys())))
        print("[ampersand] hs_lastmodifieddate=%r hs_createdate=%r" % (
            fp.get("hs_lastmodifieddate"), fp.get("hs_createdate")))
    return [hubspot_to_canonical(hs) for hs in results]
