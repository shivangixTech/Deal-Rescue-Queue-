"""Milestone 1 eval: every fixture deal must take its expected route.

Run:  python -m tests.run_eval        (from the repo root)
Also asserts the safe-failure path: if the decision call blows up,
decide() must return an error result and never raise (so no write happens).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import jev_client, policy
from src.questions import question_spec
from src.state import build_state


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    deals = {d["deal_id"]: d for d in json.load(open(os.path.join(root, "fixtures", "deals.json")))}
    cases = json.load(open(os.path.join(root, "tests", "decision-cases.json")))
    spec = question_spec()

    failures = 0
    for case in cases:
        deal_id = case["deal_id"]
        state = build_state(deals[deal_id])
        decision = jev_client.decide(state, spec)
        assert decision["mode"] in ("mock", "live"), f"{deal_id}: unexpected mode {decision['mode']}"
        result = policy.route(state, decision)
        answers = decision["answers"]

        problems = []
        if result["route"] != case["expected_route"]:
            problems.append(f"route={result['route']} (reasons: {result['reasons']})")
        for key, ans_key in (("expected_risk", "risk_band"), ("expected_action", "next_action")):
            if key in case and answers[ans_key]["value"] not in case[key]:
                problems.append(f"{ans_key}={answers[ans_key]['value']}, expected one of {case[key]}")

        status = "PASS" if not problems else "FAIL"
        if problems:
            failures += 1
        var = policy.value_at_risk(state, decision)
        print(f"[{status}] {deal_id}: route={result['route']} "
              f"risk={answers['risk_band']['value']}({answers['risk_band']['confidence']:.2f}) "
              f"action={answers['next_action']['value']} review_p={answers['needs_review']['value']:.2f} "
              f"loss_p={answers['loss_probability']['value']:.2f} value_at_risk=${var:,.0f} "
              f"mode={decision['mode']}")
        for p in problems:
            print(f"       !! {p}  ({case['why']})")

    # Prioritization check: the queue must rank savable business value first.
    # D-009 ($210K, High) > D-011 ($150K, High) > D-006 ($120K, High);
    # D-003 (already lost) must sink — nothing left to save.
    ranked = sorted(
        ((cid, policy.value_at_risk(build_state(deals[cid]),
                                    jev_client.decide(build_state(deals[cid]), spec)))
         for cid in deals),
        key=lambda t: -t[1],
    )
    order = [cid for cid, _ in ranked]
    expected_top3 = ["D-009", "D-011", "D-006"]
    if order[:3] == expected_top3 and order[-1] == "D-003":
        print(f"[PASS] prioritization: top3={order[:3]}, lost deal sinks to bottom")
    else:
        failures += 1
        print(f"[FAIL] prioritization: got order {order}")

    # Safe-failure test: a blowing-up transport must not raise and must not allow writes.
    orig = jev_client._decide_mock
    jev_client._decide_mock = lambda *a, **k: (_ for _ in ()).throw(TimeoutError("provider timeout"))
    try:
        bad = jev_client.decide(build_state(deals["D-001"]), spec)
        assert bad["mode"] == "error" and "timeout" in bad["error"].lower(), f"bad error result: {bad}"
        r = policy.route(build_state(deals["D-001"]), bad)
        assert r["route"] == "error", f"policy must surface error route, got {r}"
        print("[PASS] timeout: error result returned, no exception, no write path taken")
    finally:
        jev_client._decide_mock = orig

    # M3 write-back safety: pure-function checks, no network touched.
    from src import writeback
    from src.writeback import WritebackError

    def expect_raise(fn, label):
        try:
            fn()
        except WritebackError:
            print(f"[PASS] writeback: {label}")
            return
        except Exception as e:
            failures += 1
            print(f"[FAIL] writeback: {label} — wrong exception {type(e).__name__}: {e}")
            return
        failures += 1
        print(f"[FAIL] writeback: {label} — no exception raised")

    # 1. build_payload happy path: exactly the 4 allowlisted fields.
    wb_answers = {"risk_band": {"value": "Watch"}, "next_action": {"value": "email_followup"}}
    wb_payload = writeback.build_payload(wb_answers, "tester")
    assert set(wb_payload) == set(writeback.DRQ_FIELDS), f"payload keys: {sorted(wb_payload)}"
    assert wb_payload["drq_risk_band"] == "Watch"
    assert wb_payload["drq_recommended_action"] == "email_followup"
    assert wb_payload["drq_reviewer"] == "tester" and wb_payload["drq_reviewed_at"]
    print("[PASS] writeback: build_payload produces exactly the 4 allowlisted fields")

    # 2. build_payload with missing judgment -> refuses.
    expect_raise(lambda: writeback.build_payload({}, "tester"),
                 "build_payload refuses when Jev answers are missing")

    # 3. _hubspot_id: live IDs parse, fixture IDs refuse.
    assert writeback._hubspot_id("HS-12345") == "12345"
    expect_raise(lambda: writeback._hubspot_id("D-001"),
                 "_hubspot_id refuses fixture deal ids")
    expect_raise(lambda: writeback._hubspot_id("HS-abc"),
                 "_hubspot_id refuses malformed HubSpot ids")

    # 4. apply_writeback without approval -> refuses BEFORE any network call.
    expect_raise(lambda: writeback.apply_writeback("HS-12345", wb_payload, approved=False),
                 "apply_writeback refuses without human approval")

    # 5. apply_writeback on a fixture deal -> refuses BEFORE any network call.
    expect_raise(lambda: writeback.apply_writeback("D-001", wb_payload, approved=True),
                 "apply_writeback refuses non-HubSpot deals")

    n_writeback = 6
    print(f"\n{len(cases) + 2 + n_writeback - failures}/{len(cases) + 2 + n_writeback} checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
