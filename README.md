# Deal Rescue Queue — Ampersand + Jev demo

**Problem:** open deals go stale in the CRM and reps can't tell which one
needs attention first. **This demo:** reads live HubSpot deals through the
**Ampersand** proxy, asks **Jev** four typed questions per deal — risk band
(Score), next action (Choice), review need (Noul), loss probability (Noul) —
ranks the queue by **savable business value at risk** (amount × P(loss)),
routes uncertain cases to a **human review queue**, and writes Jev's judgment
back to HubSpot **only after explicit human approval** (guarded write-back).

## Why Ampersand / why Jev
- **Ampersand** handles the enterprise integration plumbing: customer OAuth,
  scopes, and the proxy that carries both reads and writes — so this isn't a
  one-off HubSpot script.
- **Jev** makes bounded, typed judgments with calibrated confidence that code
  can branch on — no prose parsing. Deterministic code + human review control
  every side effect.

## The loop (M1 → M2 → M3)
1. **M1 — local vertical slice:** fixtures → Jev client (live or labeled mock)
   → deterministic policy → FastAPI queue UI. Eval 13/13.
2. **M2 — Ampersand read:** live HubSpot deals flow through the Ampersand
   proxy (`?source=live`); real Jev decisions via OpenRouter's typed
   endpoint (`typesafe/jev-1.13`).
3. **M3 — guarded write-back:** two-step approval (dry-run preview →
   confirm) → real `PATCH /crm/v3/objects/deals/{id}` through the proxy,
   writing only 4 allowlisted fields. Idempotent: re-confirming an already
   written deal returns "already written" instead of a duplicate PATCH.

## Run it
```bash
pip install -r requirements.txt
python tests/run_eval.py        # 19 checks: routing + prioritization + timeout + write-back safety
uvicorn src.app:app --reload    # open http://127.0.0.1:8000
```
Toggle **Fixtures** / **Live HubSpot via Ampersand** at the top of the page.

## Environment
| Variable | Required | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | No | Real Jev judgments via OpenRouter's typed endpoint. Unset → deterministic mock, clearly labeled in UI/API/logs |
| `TYPESAFE_API_KEY` | No | Direct TypeSafe transport (tried before `OPENROUTER_API_KEY`) |
| `JEV_MODEL` | No | Model alias (default `typesafe/jev-1.13`) |
| `AMPERSAND_API_KEY` | For live | **Server or CLI** scoped key for the proxy |
| `AMPERSAND_PROJECT_ID` | For live | Ampersand project id |
| `AMPERSAND_INSTALLATION_ID` | For live | Installation id from the HubSpot OAuth install flow |

Copy `.env.example` to `.env` and fill in. Never commit keys.

### HubSpot prerequisites (for M3 write-back)
- Custom deal properties with these **exact** internal names (Single-line text):
  `drq_risk_band`, `drq_recommended_action`, `drq_reviewed_at`, `drq_reviewer`
- The connected HubSpot app needs the `crm.objects.deals.write` scope
  (in addition to the read scopes), and the Ampersand provider-app scopes
  must match the HubSpot app's scopes exactly. A fresh installation is
  required after adding a scope so the OAuth token carries it.

## Project layout
```
fixtures/deals.json        synthetic deals (clearly fake data)
tests/decision-cases.json  expected routes + why
tests/run_eval.py          eval runner: routing, prioritization, timeout, write-back safety
src/state.py               canonical state builder
src/questions.py           Jev question definitions
src/jev_client.py          live transports (TypeSafe → OpenRouter) + labeled mock adapter
src/policy.py              decision contract: thresholds -> route
src/ampersand_client.py    Ampersand proxy adapter (reads; tolerant date parsing)
src/writeback.py           M3 guarded write-back: allowlist, approval gate, idempotency
src/app.py                 FastAPI: /api/queue, /api/decide, /api/review, /api/writeback/{deal_id}
src/web/index.html         review queue UI (no build step)
integrations/amp.yaml      deployed Ampersand manifest (proxy pattern)
docs/architecture.md       design, safety rules, mocked-vs-live table
```

## API
- `GET /api/queue?source=fixtures|live` — ranked queue with Jev decisions
- `POST /api/decide` — single-deal decision
- `POST /api/review/{deal_id}?source=…` — dry-run approval preview (M2)
- `POST /api/writeback/{deal_id}?source=live` — M3 guarded write-back.
  Body: `{"approved": true, "reviewer": "…", "dry_run": true|false}`.
  `dry_run=true` (default) previews only. Real writes need
  `approved=true, dry_run=false, source=live`, and are idempotent.

## Safety policy
- Dry-run by default; nothing touches the CRM without **two** explicit human
  clicks (preview → confirm).
- Only 4 allowlisted fields (`drq_*`) may ever be written; the payload
  builder cannot produce any other key.
- Fixture deals (no HubSpot id) are refused; unapproved calls are refused
  before any network traffic.
- Re-confirming skips with "already written" — never a duplicate PATCH.
- Every preview and every write is appended to `logs/decisions.jsonl`.
- See `docs/architecture.md` for the full contract.

## Known limitations
- Thresholds are tuned on a small eval set; a real eval set is needed before
  any production claim.
- Live mode needs valid Ampersand + HubSpot credentials (see Environment).
