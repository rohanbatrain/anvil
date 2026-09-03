# How recovery is measured

The track's bar is *measured money recovered across batches*. This explains how
that number is produced and why it is smaller than it could have been.

## The number that would have been easy

Run the agent over a book of failed debits, add up what came back, report it.

That figure is worthless, and specifically so: **a large share of failed
subscription debits recover with no intervention at all**, because the customer
notices and pays. In Anvil's simulation the self-cure rate is around 20%. An
uncontrolled figure claims all of that as the agent's work.

## Three arms

Every case is assigned by **deterministic hash** to one of three arms, all run
against the same issuer model and the same customers:

| Arm | Treatment | What it establishes |
|---|---|---|
| `control` | Nothing at all | The natural self-cure rate |
| `baseline` | Fixed day 1/3/5 retries plus one identical reminder | What most merchants actually do |
| `anvil` | The full recovery graph | The thing being tested |

Assignment is a pure function of `(batch seed, case id)`, and the hash is stored,
so anyone auditing can recompute which arm a case landed in and confirm it was
not chosen after the fact to flatter the result.

The baseline matters more than the control. Beating "do nothing" is a low bar.
Beating what a competent team would build in an afternoon is the real test.

## Confidence intervals on the difference

Lift is reported as a **bootstrap confidence interval on the difference between
arms**, never as two per-arm intervals compared for overlap.

That distinction is not pedantry. Comparing intervals for overlap is a different,
weaker, and wrongly-calibrated test: two intervals can overlap while the
difference is clearly non-zero. It also runs backwards — people read
non-overlapping intervals as significance at a confidence level nobody chose. The
difference has its own sampling distribution, and that is what gets bootstrapped.
A two-proportion z-test runs alongside as a cross-check, because having a second
method is cheaper than trusting a single one.

## Saying "not significant" in those words

When an interval crosses zero the report says **NOT SIGNIFICANT**, plainly. When
the batch is additionally too small to have detected the effect being claimed, it
says that too and reports the minimum detectable effect — because "we found no
effect" and "this batch could not have found one" are different claims, and
conflating them is how underpowered experiments get presented as evidence of
absence.

## Auditing the scheduler's own claims

The report ends with a **reliability table**: predictions bucketed against
observed outcomes, plus a Brier score and expected calibration error. A system
that says "62% likely" is only useful if, across all the times it said 62%,
roughly 62% happened.

This is the instrument that produced the most useful finding in the project.

## The result

**Naive fixed-schedule dunning beats the agent on raw recovery rate** — roughly
86% against 65% — and the difference is statistically significant.

The calibration table explains why: the retry curves are over-confident by about
ten points, because they are hand-written priors rather than parameters fitted to
this issuer.

Two costs the comparison does not price, both stated in the report. The baseline
pays nothing here for burning a mandate's finite presentment allowance or for
damaging an issuer risk score — both real, and both why production dunning is
constrained in ways this baseline is not. And the batch runs with the **language
model disabled**, so unclassifiable failures fall to `UNKNOWN` and receive one
conservative attempt. Every number is a floor.

## Why it was not tuned away

Every lever needed to make the result look good was available and would have
taken twenty minutes: soften the issuer, weaken the baseline, report only lift
over control, or widen the intervals until nothing was significant.

The submission's headline number is worse for not having done that. Every *other*
number is worth something, which would not be true otherwise — a reviewer who
suspects one figure was massaged has to discount all of them.

[ADR-0012](../adr/0012-report-the-losing-result.md) records the decision.
