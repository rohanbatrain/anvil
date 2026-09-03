# ADR-0010: Recovery is measured against a control arm, or not claimed

- **Status:** Accepted
- **Date:** 2026-09-02

## Context

The track's bar is *measured money recovered across batches*. The easy way to
produce that number is to run the agent and add up what came back.

That number is worthless, and the reason is specific: **a large share of failed
subscription debits recover with no intervention at all**, because the customer
notices and pays. In our simulation the self-cure rate is around 20%. An
uncontrolled figure claims all of that as the agent's work.

## Decision

Every case is assigned by **deterministic hash** to one of three arms, all run
against the same issuer and the same customers:

- **control** — no intervention. Establishes the natural self-cure rate.
- **baseline** — industry-standard fixed-schedule dunning, day 1/3/5 plus one
  identical reminder. What most merchants actually do.
- **anvil** — the full recovery graph.

Lift is reported as a **bootstrap confidence interval on the difference**, never
as two per-arm intervals compared for overlap. A result whose interval crosses
zero is reported as *not significant*, in those words. When the batch is too
small to detect the effect being claimed, the report says so and computes the
minimum detectable effect.

## Consequences

"We recovered X" survives the question "compared to what?", which is the first
question any competent reviewer asks.

Assignment is auditable: the hash is stored, so anyone can recompute which arm a
case landed in and confirm it was not chosen after the fact.

Ten percent of cases get no intervention, which in production is real money
deliberately left on the table. That is the price of knowing whether the system
works, and it is the price every serious experimentation programme pays.

The three-arm design is what surfaced the finding in ADR-0012. A two-arm design
against control would have shown the agent winning handsomely and taught us
nothing.

## Alternatives considered

**Agent versus nothing, no baseline.** Rejected because beating "do nothing" is
a very low bar and would have hidden that a trivial fixed schedule does better.

**Comparing per-arm confidence intervals for overlap.** Rejected because it is
a different, weaker and wrongly-calibrated test: two intervals can overlap while
the difference is clearly non-zero. The difference has its own sampling
distribution, and that is what gets bootstrapped. Stated in the docstring of
`anvil/evidence/statistics.py` so nobody re-introduces it.

**A historical backtest instead of a control arm.** Rejected because there is no
production history, and because a backtest cannot separate "the agent worked"
from "conditions were better that month".
