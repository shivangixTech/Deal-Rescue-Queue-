# Deal Rescue Queue - Project Explainer

## 1. The idea

Sales teams lose track of open deals in the CRM. A deal's stage sits in one
place, its notes in another, and its recent activity somewhere else, so a rep
cannot tell **which deal needs action first**.

Deal Rescue Queue is a rescue queue for deals that are quietly dying:

> Connect HubSpot, analyze the open deals, surface the risky ones at the top
> with a recommended next step for each, send uncertain cases to human
> review, and write back to the CRM only after a human approves.

## 2. What was built

The system was built in three milestones - local slice first (M1), live
HubSpot reads through Ampersand (M2), then guarded write-back (M3) - so every
stage was a working, tested system.

| Component | File | Role |
|---|---|---|
| Sample deals | `fixtures/deals.json` | Clean and deliberately confusing cases, so testing covers both |
| State builder | `src/state.py` | Deal -> small canonical object (stage, amount, idle days, note). This is all Jev ever sees |
| Typed questions | `src/questions.py` | 4 questions in one call: risk Score, next-action Choice, review Noul, loss-probability Noul |
| Jev client | `src/jev_client.py` | Real Jev when a key is set, clearly labeled mock otherwise. Every result carries its mode |
| Policy | `src/policy.py` | Thresholds and rules -> `human_review` or `recommendation_ready` |
| Ampersand client | `src/ampersand_client.py` | Live HubSpot deal reads through the Ampersand proxy |
| Write-back | `src/writeback.py` | Guarded PATCH to HubSpot: allowlisted fields only, idempotent, approval required |
| API | `src/app.py` | `/api/queue`, `/api/decide`, `/api/review` (dry-run preview, writes nothing) |
| UI | `src/web/index.html` | The queue: Critical to Healthy, confidence bars, approve actions |
| Tests | `tests/run_eval.py` | 19 checks covering routing, prioritization, failure safety, and write-back safety. All green |

## 3. How it works, step by step

1. **A deal arrives** - from fixtures in M1, from HubSpot via Ampersand in the
   live system.
2. **State is built** - only the fields that matter, in one small object. A
   missing value stays `null`; values are never invented.
3. **One Jev call per deal** - four independent questions on the same state:
   - *risk_band* (Score): Healthy / Watch / High / Critical, with confidence
   - *next_action* (Choice): call_now / email_followup / nurture /
     review_close_lost / human_review, with confidence
   - *needs_review* (Noul): a 0 to 1 probability - "should a human look at
     this?"
   - *loss_probability* (Noul): "if nothing happens for 14 days, how likely is
     this deal to be lost?" - 0 to 1
4. **Deterministic policy routes the deal**, in this order: missing context ->
   review. needs_review >= 0.65 -> review. Confidence < 0.60 -> review.
   Action `review_close_lost` (closing a deal is irreversible) -> review.
   Otherwise the recommendation is ready.
5. **Value at risk is computed** - `amount x loss_probability`. A deal that is
   already lost has a savable value of 0 (there is nothing left to rescue), so
   it sinks to the bottom.
6. **The UI shows the queue sorted by value at risk**, highest savable value
   first. Every card shows "$X at risk", and review cards are highlighted
   separately.
7. **A human approves** - first a dry-run preview shows exactly what would be
   written, then a separate confirmation sends the real write to HubSpot.

**The most important line in the design:** Jev never takes an action. It only
returns judgments - typed, with confidence. Every action is controlled by
deterministic code (thresholds) and a human (approval).

## 4. What is Jev?

A normal LLM writes text. That text has to be parsed, and the model can go off
the rails at any point. **Jev does not write text.** You send it a state and
typed questions, and it returns code-usable answers directly: a choice, the
probability of each option, and a confidence score. That is what makes it
usable inside automation - it is fast (milliseconds), cheap, and every answer
says how much the model trusts itself.

In this project Jev is reached through a fallback chain: a direct TypeSafe key
if one is set, otherwise OpenRouter's typed endpoint (model
`typesafe/jev-1.13`), otherwise a clearly labeled mock. The rest of the system
does not change when the transport changes.

## 5. What is Ampersand?

Ampersand is the integration layer between this app and HubSpot: OAuth login,
reading deals and notes, writing updates back, and mapping each account's
field names onto one canonical schema. The app talks to HubSpot through the
Ampersand proxy, so swapping the data source (fixtures -> live HubSpot) changed
nothing in the state, policy, or UI code.

## 6. Design decisions

**The two-line summary:** Jev is used for bounded judgment, not orchestration.
Ampersand handles the enterprise integration; deterministic code plus human
review controls every action.

**Why not a normal LLM?** The use case has fixed answer spaces: a risk band, a
next action. Jev returns typed values and probabilities directly, so there is
no prose to parse and nothing to misread. If generative output were needed, an
LLM could be added beside it - it is just not the right tool for the decision
itself.

**How were the thresholds chosen?** They started as hypotheses, were tested
against labeled synthetic cases, and were tuned for one goal above all: zero
unsafe automatic actions. Uncertain cases go to human review on purpose.

## 7. How the design meets its goals

- **Safe automation** - fully built. Jev never acts; policy, human approval,
  a dry-run preview, and an audit log stand between a judgment and any write.
  Write-back is limited to 4 allowlisted fields and is idempotent: approving
  the same deal twice writes once.
- **Risk detection** - a risk Score plus a loss-probability Noul per deal,
  computed from live deal state in the final system.
- **Action recommendation** - every deal leaves with a concrete next action,
  based on its state and recent activity.
- **Prioritization by value** - the queue is sorted by `value_at_risk`, not by
  deal size or age alone. This is what separates it from "one more list": the
  biggest deal is not automatically the most urgent one.
