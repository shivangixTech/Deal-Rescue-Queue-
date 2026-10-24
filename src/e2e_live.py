"""End-to-end live test (Milestone 2, Step 9).

Reads the sample deals from HubSpot through the Ampersand proxy and runs
every deal through the SAME pipeline the queue UI uses:

    fetch_deals() -> build_state() -> decide() -> route() -> value_at_risk()

then prints the queue ranked exactly the way the UI ranks it.

READ-ONLY: this script WRITES NOTHING to HubSpot. The Jev mode shown
(mock vs live) tells you whether TYPESAFE_API_KEY is set.

Same environment variables as src/ampersand_adapter.py:
    AMP_API_KEY          Ampersand API key of type "Server or CLI"
    AMP_PROJECT_ID       Ampersand project ID (Project settings page)
    AMP_INSTALLATION_ID  Installation ID from the install flow (Step 6)

Quick check (Windows cmd):
    set AMP_API_KEY=... & set AMP_PROJECT_ID=... & set AMP_INSTALLATION_ID=...
    python -m src.e2e_live
"""

import sys

from . import jev_client, policy, state
from .questions import question_spec


def decide_live_deal(deal):
    """Run one live deal through the queue pipeline. Never writes anywhere."""
    deal_state = state.build_state(deal)
    decision = jev_client.decide(deal_state, question_spec())
    result = policy.route(deal_state, decision)
    return {
        "deal_id": deal_state.get("deal_id"),
        "deal_name": deal_state.get("deal_name"),
        "company": deal_state.get("company"),
        "stage": deal_state.get("stage"),
        "amount": deal_state.get("amount"),
        "decision": decision,
        "route": result,
        "value_at_risk": policy.value_at_risk(deal_state, decision),
    }


def main():
    # Lazy import: keeps the env-var error message exactly the adapter's.
    from . import ampersand_adapter

    try:
        deals = ampersand_adapter.fetch_deals()
    except RuntimeError as exc:
        print("error: " + str(exc), file=sys.stderr)
        raise SystemExit(1)

    items = [decide_live_deal(d) for d in deals]
    items.sort(key=policy.sort_key)

    print(f"\n{len(items)} live deals -> queue (read-only; nothing written to HubSpot)")
    print("-" * 118)
    print(f"{'#':<3}{'deal':<28}{'company':<18}{'stage':<13}{'amount':>10}  "
          f"{'risk':<9}{'action':<14}{'route':<20}  value_at_risk")
    print("-" * 118)
    for i, item in enumerate(items, 1):
        ans = (item["decision"].get("answers") or {})
        mode = item["decision"].get("mode", "?")
        risk = (ans.get("risk_band") or {}).get("value", item["decision"].get("error", "ERR"))
        action = (ans.get("next_action") or {}).get("value", "-")
        route = item["route"].get("route", "-")
        amount = item["amount"]
        amount_s = f"${amount:,.0f}" if amount else "-"
        print(f"{i:<3}{str(item['deal_name'])[:27]:<28}{str(item['company'] or '-')[:17]:<18}"
              f"{str(item['stage'] or '-')[:12]:<13}{amount_s:>10}  "
              f"{str(risk):<9}{str(action)[:13]:<14}{route:<20}  ${item['value_at_risk']:,.2f}")
    print("-" * 118)

    review_n = sum(1 for i in items if i["route"]["route"] == "human_review")
    total_var = sum(i["value_at_risk"] for i in items)
    modes = sorted({i["decision"].get("mode") for i in items})
    print(f"human_review: {review_n}/{len(items)} | total value at risk: ${total_var:,.2f} "
          f"| jev mode: {', '.join(modes)}")
    if "mock" in modes:
        print("note: decisions are MOCK (deterministic rules) — set TYPESAFE_API_KEY for live Jev.")
    print("READ-ONLY: nothing was written to HubSpot.\n")


if __name__ == "__main__":
    main()
