# How to fit the retry curves to real outcomes

The retry curves in `anvil/domain/taxonomy.py` are **hand-written priors**. The
calibration report says they are systematically over-confident by about ten
points, and that is the main reason the agent currently loses to naive dunning.
This is how you fix it.

## 1. Measure what you have

```bash
make batch
```

Read the **Is the scheduler honest?** section. The reliability table buckets
predictions against observed outcomes:

```
      band       n   predicted    observed       gap
    30-40%      25       37.2%       16.0%    +21.2%
    40-50%      35       44.5%       25.7%    +18.8%
```

A positive gap is over-confidence: it promised more than it delivered. Expected
calibration error is the bucket-weighted average gap, and the Brier score
punishes confident mistakes far more than hedged ones — which is the right
incentive for something that spends a finite number of retry attempts.

## 2. Collect predictions and outcomes

Every `PaymentAttempt` stores `predicted_probability_bps` alongside its actual
outcome, so the data is already there. In code:

```python
from anvil.risk.calibration import Prediction, calibrate

report = calibrate([
    Prediction(attempt.predicted_probability_bps, bool(attempt.succeeded))
    for attempt in attempts
])
print(report.verdict)
```

`calibrate` refuses to draw conclusions below 100 attempts and says so, because
a calibration report over twelve attempts is noise and presenting noise as
evidence is the dishonesty this whole module exists to avoid.

## 3. Refit

The curves are a product of four independent factors — an attempt base rate, an
age factor, a circadian factor and a salary-cycle factor. Fit them **per factor**
rather than jointly: with realistic data volumes a joint fit will overfit the
interactions, and the factors are separately interpretable, which matters when a
merchant asks why their retry moved.

Group observed outcomes by failure class, then by each factor's bucket, and take
the empirical rate. Start from the priors and shrink toward them in proportion to
how little data each bucket has — a bucket with nine observations should barely
move.

## 4. Re-measure

```bash
make batch
```

Expected calibration error should fall. The number that actually matters is the
**lift against the baseline arm**, because well-calibrated probabilities that
produce the same decisions have changed nothing.

## What to do about a merchant with no history

The hard case, and worth thinking about before you are asked. A new merchant has
no outcomes to fit on. Three options, in increasing order of ambition:

1. Use the shipped priors until there is enough data. Honest, and what happens
   today.
2. Fit on the pooled population across merchants, then shrink toward that pool as
   a merchant accumulates its own history. Standard hierarchical shrinkage.
3. Fit per issuer rather than per merchant, since the hazard is mostly a property
   of the bank. Probably the best answer, and it needs enough merchants to be
   worth doing.

## Where the ceiling is

The scheduler cannot beat the information in its curves. If a bank's behaviour is
genuinely unpredictable from the features available — class, attempt number, age,
hour, day of month — then no amount of fitting helps, and the honest move is to
report that the optimiser adds nothing for that issuer rather than to add
features until something correlates.
