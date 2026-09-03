# How to run the batch experiment

```bash
make batch
```

Three arms against the same issuer and the same customers, assigned by
deterministic hash. Takes a few seconds; needs no database or credentials.

## Change the size or the seed

```bash
make batch SEED=20260902 SIZE=5000
```

A larger population means more at-risk cases and tighter confidence intervals.
The same seed always reproduces the same result, byte for byte.

## Measure what the language model is worth

```bash
make batch-with-model
```

Runs the same experiment with the LLM classifier modelled as available at 88%
accuracy — deliberately not an oracle, so the measured benefit includes the cost
of the model being wrong. The difference between the two runs is the model's
contribution to recovery, measured rather than asserted.

## Get the numbers as JSON

```bash
.venv/bin/python -m anvil.evidence.run_batch --json out.json
```

Or from the running console API:

```bash
curl 'http://localhost:8000/api/batch?size=2000&with_model=false'
```

## Use a production-shaped split

The default is an even three-way split, because a 10% holdout on a few hundred
cases gives intervals too wide to conclude anything.

```bash
.venv/bin/python -m anvil.evidence.run_batch --split production
```

That holds back 10% for control and 10% for baseline, which is what you would
actually run against live traffic.

## Reading the output

**Per-arm outcomes** — recovery rate with a bootstrap confidence interval.

**Lift** — always a bootstrap interval on the *difference*, never two per-arm
intervals compared for overlap, which is a different and weaker test. When the
interval crosses zero the report says **NOT SIGNIFICANT** in those words, and if
the batch was too small to detect the claimed effect it says that too and gives
the minimum detectable effect.

**Is the scheduler honest?** — a reliability table bucketing predictions against
observed outcomes, plus a Brier score and expected calibration error. This is the
audit of the scheduler's own claims.

**What this run does not show** — the limitations, in full. Read them.

## The result you should expect

**Naive fixed-schedule dunning currently beats the agent** on raw recovery rate,
significantly. That is not a bug in the harness; it is the finding, and
[ADR-0012](../adr/0012-report-the-losing-result.md) explains why it is reported
rather than tuned away. The calibration table underneath is the diagnosis.
