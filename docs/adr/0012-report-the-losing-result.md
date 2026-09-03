# ADR-0012: Report the result that makes the project look worse

- **Status:** Accepted
- **Date:** 2026-09-02

## Context

The batch experiment produced a result I did not want. **Naive fixed-schedule
dunning beats the agent on raw recovery rate — roughly 86% against 65% — and the
difference is statistically significant.**

Every lever needed to make that go away was within reach and would have taken
about twenty minutes: soften the issuer model, drop the baseline to two retries,
report only "lift versus doing nothing", or quietly widen the confidence interval
until nothing was significant.

## Decision

Report it. The batch report leads with the loss, the README states it, this ADR
records it, and `REVIEWING.md` points a reviewer straight at it.

Alongside it, report the *diagnosis* rather than an excuse. The calibration table
shows the retry curves are systematically over-confident by about ten points
(expected calibration error 11.4%), because they are hand-written priors rather
than parameters fitted to this issuer. `anvil/risk/calibration.py` is the
mechanism for fixing that, and it has not been run against real outcomes.

The report also states two costs the comparison does not price: the baseline pays
nothing here for burning a mandate's finite presentment allowance or for damaging
an issuer risk score, and the batch runs with the language model **disabled**, so
unclassifiable failures fall to `UNKNOWN` and get one conservative attempt. Every
number is a floor.

## Consequences

The submission's headline number is worse than it could have been.

Every *other* number in the repository is worth something, which would not be
true if this one had been tuned. A reviewer who suspects one figure was massaged
has to discount all of them.

The weakness is now specific, measured and actionable, which is a far better
position than a flattering number and no idea why it is flattering. "Our curves
are miscalibrated by ten points and here is the instrument that measures it" is
a stronger engineering statement than "our agent recovered 86%".

The rubric for this track explicitly asks for honest metrics including the cost
of false positives. This is that, applied to ourselves.

## Alternatives considered

**Tune the simulator until the agent wins.** Rejected. It would have made the
experiment a demonstration of nothing, and it is the exact failure mode the
control arm in ADR-0010 exists to prevent.

**Report only lift over control, omitting the baseline.** Rejected as the most
tempting option, because it is true, favourable, and materially misleading —
+42 points over doing nothing sounds excellent right up until someone asks what a
fixed schedule would have achieved.

**Delay the experiment until the curves are fitted.** Rejected because the
deadline is real and an unmeasured system is worse than a measured one that
currently loses.
