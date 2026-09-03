# ADR-0004: Bounded commercial authority under a hard-capped budget

- **Status:** Accepted
- **Date:** 2026-09-02

## Context

The track asks for "bounded recovery workflows", and *bounded* is doing a lot of
work in that phrase. The safe reading is that the agent may only send reminders
and retry debits. The interesting reading is that it has real commercial
authority — it can concede something to save a subscription — constrained by
explicit limits.

The safe reading has a problem: with no commercial lever, the decision collapses
to retry timing, which is deterministic. There is then no judgement left for a
model to exercise, and the case for using one at all evaporates.

Razorpay's own Agent Studio guardrails forbid agents "generating unauthorised
discounts". The operative word is *unauthorised*.

## Decision

The agent may draw on **merchant-authorised commercial levers** — grace periods,
partial payment, plan downgrade, a capped win-back discount — against a
per-merchant concession budget with per-customer, per-action and
percentage-of-MRR ceilings, all enforced **deterministically outside the model**.

Three independent mechanisms bound it, and each fails closed:

1. The mandate registry refuses an action with no valid authorisation.
2. The policy engine caps the amount at the tighter of a rupee ceiling and a
   percentage of the subscription's monthly value.
3. The ledger reserves against a budget row under `SELECT … FOR UPDATE`, so two
   concurrent cases cannot jointly overspend.

## Consequences

The decision problem becomes real: *is this customer worth ₹200 to save a
₹1,499/month mandate?* That depends on churn risk, tenure, prior concessions and
how they responded last time — judgement under constraint, which is what a model
is for.

"Bounded" becomes a precondition of execution rather than a convention someone
can forget, and each ceiling reports itself as the limiting one so an operator
knows which policy stopped the agent.

We accept a real risk: a reviewer skimming may read "the AI gives away money".
The mitigation is that the budget is funded explicitly by the merchant and held
in the ledger as a restricted asset, so overspending is arithmetic that cannot
happen rather than a rule that might not fire.

The deterministic fallback path **never offers a concession**, because pricing
one was exactly the judgement the model was there to make.

## Alternatives considered

**Recovery actions only, no commercial levers.** Safest, and briefly tempting.
Rejected because it makes the LLM unnecessary, which would have been an honest
outcome but a much less interesting system — and the rubric explicitly rewards
bounded *authority*, not bounded *ambition*.

**Autonomous plan renegotiation** — letting the agent restructure the
subscription itself. Rejected because mandate amendment under UPI Autopay and
e-NACH has real re-authorisation mechanics that cannot be demonstrated
convincingly in test mode, so it would have been theatre.

**A policy-only limit with no ledger reservation.** Rejected because a policy
check and the spend are not atomic: two cases can both pass a headroom check
before either records its draw.
