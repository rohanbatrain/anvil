# ADR-0005: Retry timing is a dynamic program, not a model call

- **Status:** Accepted
- **Date:** 2026-09-02

## Context

"When should I retry this failed debit?" is the most obviously agentic-sounding
question in the whole domain, and the most tempting thing to hand to a language
model. The track's rubric explicitly penalises exactly that instinct: applying
an LLM where deterministic logic already suffices.

It is also not the question it appears to be. The real question is *"given a
finite number of retry attempts against this mandate, when should I spend the
next one?"* — a sequential decision problem. Treating it greedily is how dunning
systems burn three attempts in the 48 hours after a failure and have nothing
left for payday.

## Decision

Retry timing is solved as a **dynamic program over a tabulated hazard function**,
with no model involved and no escalation path to one. With `A` the amount at
risk and `p(k,t)` the chance the *k*-th remaining attempt settles at hour `t`:

```
V(0, t) = 0
V(k, t) = max over t' >= t of [ p(k,t')·A + (1 − p(k,t'))·V(k−1, t' + gap) ]
```

The expression inside the max does not depend on `t`, so `V(k,·)` is a **suffix
maximum** — one backward pass per level, making the solve `O(attempts × horizon)`
rather than quadratic. A few thousand exact `Decimal` operations, fast enough to
run inline on every case and simple enough to check by hand.

## Consequences

The behaviour is defensible and demonstrable. From a mid-cycle failure:
`insufficient_funds` waits eleven days to reach a salary-credit day;
`issuer_technical` retries in six hours; `instrument_expired` and `risk_declined`
are refused outright with reasons.

It is reproducible. The same inputs give the same answer on every machine, which
is what lets the batch experiment mean anything.

The argmax also yields the value of the remaining attempts, which the planner
needs when weighing "keep retrying" against "offer a concession". That number
would not exist if a model had been asked for a date.

**The cost is that the scheduler is only as good as its curves.** The curves are
hand-written priors, and the calibration report says they are systematically
over-confident by about ten points against our own issuer model. That is a real
weakness, it is measured rather than hidden, and `anvil/risk/calibration.py` is
the mechanism for fixing it.

## Alternatives considered

**Asking the model for a retry time.** Rejected as slower, less accurate, and —
fatally for a submission claiming reproducibility — non-deterministic.

**A fixed schedule (day 1, 3, 5).** This is the baseline arm in the experiment,
so it is measured rather than dismissed. In the current simulation it *beats*
the optimiser, for the reasons in ADR-0012.

**A learned model fitted at runtime.** The right long-term answer and the
direction calibration points. Rejected for now because there is no production
outcome data to fit on, and a model fitted to a simulator would only prove the
simulator.
