# Deal Rescue Queue — Architecture (Milestone 1)

## Problem
Sales teams lose track of open deals: stage, amount, notes, and recent
activity live in different places, so reps don't know which deal needs
attention first.

## Promise
Connect HubSpot, analyze open deals, surface risky opportunities with a
recommended next step, and safely write approved triage back to the CRM.

## Control flow (M1: fixtures stand in for Ampersand)

```
fixtures/deals.json ──▶ state.py ──▶ jev_client.py ──▶ policy.py ──▶ app.py ──▶ web UI
 (HubSpot via        (canonical     (Jev: Choice +     (thresholds +   (FastAPI +     (review
  Ampersand in M2)    state v1)       Score + Noul)      routing)        dry-run)       queue)
                                        │                   │
                                        ▼                   ▼
                                   logs/decisions.jsonl (audit trail)
```

## Responsibility boundaries
| Layer | Owns | Must NOT own |
|---|---|---|
| Ampersand (M2) | Connection, data movement, field mappings, supported CRM operations | Business interpretation of the data |
| Jev | Narrow typed judgments (risk band, next action, review need) against the supplied state | Workflow control, explanations, any side effect |
| Our code | State shaping, thresholds, deterministic guards, audit log, dry-run write policy | Hiding uncertainty, inventing missing context |
| Human | Approval for uncertain / high-impact cases, overrides | Reviewing every obvious low-risk case |

## Decision contract
- **State** (`src/state.py`, v1): deal_id, stage, amount, close_date,
  days_since_activity, latest_note (trimmed), activity_summary (newest 5).
  Missing values are explicit `null`.
- **Questions** (`src/questions.py`, v2): `risk_band` (Score over
  Healthy/Watch/High/Critical), `next_action` (Choice of 5), `needs_review`
  (Noul: "context missing/contradictory/insufficient"), `loss_probability`
  (Noul: "P(deal stalls or is lost if nothing happens in 14 days)").
- **Policy** (`src/policy.py`, v1): missing context → review;
  needs_review ≥ 0.65 → review; any confidence < 0.60 → review;
  `review_close_lost` → review (closing is irreversible); else ready.
- **Prioritization**: `value_at_risk = amount × loss_probability` (0 for
  already-lost deals — nothing left to save). The queue is ranked by savable
  value, not by risk band alone: this is what makes it a prioritization tool
  rather than a sorted list. A quiet $95K Watch deal outranks a noisy $6K
  High deal, because action there saves more business value.
- Every decision logs: correlation id, deal id, question/policy versions,
  model, probabilities, route + reasons, value at risk, timestamp.

## Safety rules (M1)
- Dry-run is the default: `/api/review` previews the write payload and writes nothing.
- Only allowlisted fields may ever be written (see amp.yaml draft).
- Delete is disabled; stage/amount/owner are never touched; no customer emails.
- Low confidence always becomes human review. Target: zero unsafe auto-actions.
- UI shows the source of every decision (mode banner: MOCK vs LIVE).

## What is mocked vs live in M1
| Piece | Status |
|---|---|
| HubSpot read via Ampersand | MOCKED — `fixtures/deals.json` stands in (M2) |
| Jev judgments | MOCKED until `TYPESAFE_API_KEY` is set — deterministic rules, clearly labeled |
| Policy routing, queue UI, eval | LIVE code |
| CRM write-back | DRY-RUN preview only (M3) |

## Next steps
- **M2 — Ampersand read path:** project, provider app, deploy amp.yaml (verify
  keys against live docs), InstallIntegration, normalize HubSpot records into
  the canonical state.
- **M3 — Guarded write-back:** real write of the 4 allowlisted fields behind
  the existing approval step; idempotency keys; write receipts in the audit log.
- **M4 — Polish:** architecture diagram, demo video, broader eval, known
  limitations, second CRM or subscriptions.
