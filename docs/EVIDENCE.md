# Anvil — Evidence Methodology

The reference for `make batch`: what the experiment claims, how it is constructed, what
the simulator behind it does and does not model, which statistics are computed, and where
the whole thing stops being evidence.

Every statement here describes code in this repository. Where a number appears it is from
a run named in the text, not from an estimate.

Read alongside `docs/ARCHITECTURE.md` §11, which states the design intent. This document
states the implementation.

---

## 1. The claim under test

The track's bar is *measured money recovered across batches*. A gross recovery figure does
not meet that bar, because the question immediately behind it is **"would those payments
have succeeded anyway?"** A meaningful fraction of failed subscription debits recover with
no intervention at all: the customer sees the bank message and pays.

So the experiment is built to support exactly one narrow claim:

> On a seeded synthetic book, the recovery rate of the Anvil arm differs from the recovery
> rate of a no-intervention control arm and from that of a fixed-schedule dunning arm, by
> an amount whose 95% bootstrap confidence interval is reported alongside it.

Three things that claim deliberately is not:

- It is **not** a claim about production traffic. Every outcome comes from
  `anvil/simulator/`, described in §4.
- It is **not** a claim about money in the general case. The money columns are exact
  arithmetic over simulated amounts, and no interval is computed for any money figure.
- It is **not** a claim that Anvil beats fixed-schedule dunning. At the default seed it
  does not. See §8 and §9.

---

## 2. Experimental design

### 2.1 Unit of analysis

One **at-risk case**: a single subscription whose debit was presented once to the issuer
model and declined. Cases are produced by `World.open_cases`
(`anvil/simulator/world.py`), which presents every subscription in the book exactly once
and keeps the failures. Nothing is hand-placed into the failure set — the failure mix is
whatever the issuer's own parameters produce.

The sample size is therefore **not** `--size`. `--size` is the number of subscriptions in
the book; the sample is the subset that failed. At `--seed 20260902 --size 3000` that is
**608 cases from 3,000 subscriptions (20.3%)**.

### 2.2 The three arms

Defined in `anvil/simulator/world.py`.

| Arm | What it does | Code |
|---|---|---|
| `control` | Nothing is sent, nothing is retried. A single Bernoulli draw at `intent_to_pay × ability_to_pay × 0.55` decides whether the customer self-cures. Cases whose true class is in `_TERMINAL_FOR_DEBIT` are marked `UNRECOVERABLE` and cannot self-cure. | `run_control` |
| `baseline` | Fixed-schedule dunning. One reminder on day 1, in English, from a fixed template that does not address the failure cause. Then retries on days 1, 3 and 5, each at 06:00 UTC, without consulting the decline code — so it spends attempts on terminal classes too. Charged 25 paise for its one message. | `run_baseline` |
| `anvil` | The real recovery graph (`anvil/graph/build.py`) driven through its twelve ports, with the real classifier, scheduler, scoring, policy engine and mandate check behind them. | `run_anvil` |

`_TERMINAL_FOR_DEBIT` is `{INSTRUMENT_EXPIRED, MANDATE_REVOKED, ACCOUNT_CLOSED,
RISK_DECLINED}`. Note that the baseline still *counts* an attempt against a terminal case
without presenting it, which is the flaw the baseline exists to demonstrate.

All three arms are run against the **same** issuer instance and the **same** customers,
seeded identically. The arms differ only in what is done to a case.

### 2.3 Assignment

`anvil/evidence/assignment.py`. The arm is a pure function of the batch seed and the case
id, computed before any outcome is known:

```
digest = BLAKE2b-256( str(seed) || 0x1F || case_id )     # assignment_hash
bucket = int(digest, 16) % 10000                          # bucket_of
arm    = the arm whose half-open bucket range contains it # ArmSplit.arm_for_bucket
```

Four properties follow, and each is a deliberate choice:

- **The digest is stored, not just the arm.** `Assignment` carries `assignment_hash` and
  `bucket` as well as `arm`, so an auditor can recompute every assignment from the seed
  and the case ids alone. `assignment.verify()` does exactly that and checks all three
  fields.
- **Buckets, not floats.** 10,000 buckets against a split expressed in basis points over
  the same 10,000. There is no floating-point threshold in the assignment path.
- **Arm order is fixed: control, baseline, anvil.** Control occupies the lowest buckets,
  so growing the anvil arm at the baseline arm's expense leaves every control case where
  it was, and two batches at different splits share a comparable holdout.
- **Order-independence.** `assign(seed, case_id)` does not depend on how many cases were
  assigned first, so a batch can be assembled incrementally without disturbing anything
  already decided. `assign_all` refuses duplicate ids rather than collapsing them, because
  a duplicate would inflate an arm's denominator.

Modulo bias from folding a 256-bit digest into 10,000 buckets is bounded by `10000/2**256`
— about 1e-73, and documented as such in `bucket_of`.

### 2.4 Splits

| Constant | control / baseline / anvil | Selected by |
|---|---|---|
| `EVEN_SPLIT` | 33.34% / 33.33% / 33.33% | `--split even` (**the CLI default**) |
| `DEFAULT_SPLIT` | 10% / 10% / 80% | `--split production` |

`ArmSplit.__post_init__` refuses any split that does not sum to 10,000 bps, because a
split that does not sum to one leaves part of the population with no arm at all.

`make batch` does not pass `--split`, so **`make batch` runs the even split.** The
production split exists to model what a merchant would actually hold back; at realistic
batch sizes its 10% holdout gives intervals too wide to conclude much (§8.4).

The report does not print which split was used. The only way to tell from the output is
to read the per-arm `n` column.

### 2.5 Seeding and determinism

One integer seed drives everything. `anvil/simulator/rng.py` makes that reproducible
rather than merely repeatable:

- **Substreams, not one shared generator.** `substream(seed, *labels)` hashes the seed
  with a label set and returns a `random.Random` keyed to it. Outcomes depend on
  `(seed, label)` only, so the world can process events in any order, and the issuer can
  be queried out of band, without perturbing anything.
- **Integers and Decimals, never transcendental floats.** `bernoulli` compares an integer
  draw against a probability scaled to parts per million. `skewed_int` is the minimum of
  *k* uniforms rather than a log transform. `random.random()` and IEEE-754 arithmetic are
  bit-reproducible across platforms; `log`, `exp` and `gauss` route through libm and are
  not.
- **Deterministic ids.** `deterministic_id` (`anvil/core/ids.py`) derives ULID-shaped ids
  from a BLAKE2b digest of their inputs, so the same seed produces byte-identical case,
  customer and subscription ids.
- **An injectable clock.** Nothing calls `datetime.now()`. The anvil arm runs on a
  `FrozenClock` (`anvil/core/clock.py`) seeded at the case's failure instant, and the
  scheduler adapter advances that clock to the hour the optimiser picked. Without that
  advance every arm would present its retries at the moment of failure and the
  scheduler's choice of hour would have no effect on anything.
- **A fixed epoch.** `BATCH_EPOCH = 2026-09-01 06:00 UTC`, a module constant in
  `run_batch.py` rather than a clock read, so a run in December reproduces a run in
  September exactly.

`tests/unit/test_simulator.py` enforces the claim: `test_the_same_seed_builds_the_same_population`
compares a `Population.fingerprint()` digest, and `test_a_whole_batch_is_reproducible`
compares case ids, recovered amounts and statuses across two full runs.

**One documented exception.** `metrics.aggregate` seeds each arm's rate interval with
`seed + arm.value.__hash__() % 1000`. Python randomises string hashing per process unless
`PYTHONHASHSEED` is set, so **the per-arm confidence-interval bounds are not stable across
processes.** Two runs of the same batch on this machine produced control-arm intervals of
`[14.36%, 25.97%]` and `[14.35%, 25.97%]` over identical data. Point estimates are
unaffected (they are `successes/trials`), and every comparison interval is unaffected
(`compare` seeds the bootstrap from `seed` directly). Setting `PYTHONHASHSEED=0` removes
the variation.

### 2.6 What the seed does not separate

`--seed` seeds the population, the issuer, the customer model, the arm assignment and the
bootstrap. It is one number. There is no way to hold the world fixed and re-randomise the
assignment, so the variance contributed by assignment alone cannot be isolated or bounded
from the CLI.

---

## 3. Sample size and power

The batch does not target a sample size. It generates a book, presents it once, and works
whatever fails.

At `--seed 20260902 --size 3000`, even split: 608 cases, split 181 / 211 / 216.

Power is reported rather than planned. `minimum_detectable_effect_bps`
(`anvil/evidence/statistics.py`) is computed for every comparison and printed whenever a
result is not significant:

```
effect = (z_alpha + z_beta) * sqrt( 2·p·(1−p) / n_per_arm )
z_alpha = 1.96,  z_beta = 0.84 (power = 0.80)
p       = the comparator arm's observed rate
n       = min(treatment.case_count, against.case_count)
```

The `power` argument admits only two values in practice: `<= 0.80` gives `z_beta = 0.84`,
anything above gives `1.28`. Nothing in the codebase passes anything but the default.

A `Comparison` is flagged `underpowered` when it is not significant **and** the absolute
point difference is smaller than the MDE — the distinction between "we found no effect"
and "this batch was too small to find one".

---

## 4. What the simulator models

Three files: `population.py` generates the book, `issuer.py` decides whether a debit
settles, `customer.py` decides how a person responds to a message.

### 4.1 The population — `anvil/simulator/population.py`

Pure function of the seed, no I/O.

| Dimension | Distribution |
|---|---|
| Price points | ₹99 to ₹4,999, weighted, concentrated in the ₹99–₹499 band |
| Language | 8 languages, `en` 42%, `hi` 26% |
| Mandate type | UPI Autopay 54%, e-NACH 24%, card 14%, Reserve Pay 5%, delegated agent 3% |
| Tenure | 5–1,400 days, skewed young (min of 3 uniforms) |
| Ability to pay | 16% of customers draw `U(0.15, 0.45)`; the rest `U(0.55, 0.99)` |
| Intent to pay | `U(max(0.10, ability − 0.35), 0.99)` — correlated with ability, not equal to it |
| Mandate ceiling | 1.2× to 3.0× the subscription amount |
| Attempts per cycle | 3 (60%), 4 (30%), 2 (10%) |
| Churn under pressure | 12% of customers will walk rather than be chased |

Plans form a four-tier ladder so a downgrade always has somewhere to go. Names are drawn
from real Indian given and family names so nothing in a demo reads as "John Doe".

### 4.2 The issuer — `anvil/simulator/issuer.py`

An **additive hazard model**. Each term is the probability that one specific thing goes
wrong, so a healthy debit lands near 1.0 by default rather than by cancellation of
multipliers:

```
balance_hazard = (1 − ability)^1.5 · (2 − salary_factor(day)) · 0.80
rail_hazard    = (1 − availability(hour, bank)) + max(0, 1.10 − bank.reliability)·0.30
fatigue_hazard = attempts_this_cycle · 0.04
P(settle)      = clamp( 1 − (balance + rail + fatigue + 0.015) ) · idiosyncrasy(bank, day)
```

- **Salary cycle.** `_salary_factor` runs 1.38 at the turn of the month down to 0.71
  around the 20th. It agrees in *shape* with the taxonomy's retry curves and differs in
  amplitude and trough position — deliberately, so the scheduler is recovering structure
  through noise rather than reading its own answer back.
- **Maintenance window.** `_rail_availability` drops to `1 − maintenance_severity` for
  01:00–04:00 IST, half that penalty at 00:00 and 05:00, and 98.5% during business hours.
- **Per-bank personality.** Eight simulated banks with persistent reliability multipliers
  (0.76 to 1.06), maintenance severities and reason-code dialects (`upi`, `nach`, `card`,
  `text`). A weak issuer is weak every time.
- **Per-bank, per-day wobble.** `U(0.96, 1.04)`, redrawn per `(bank, day)` rather than per
  attempt, so it behaves like an operational condition rather than white noise.
- **Terminal conditions short-circuit to zero.** A revoked mandate does not settle with 3%
  probability; it does not settle.
- **Reason strings.** 20% of failures (`UNMAPPED_CODE_SHARE`) carry free text no code
  table recognises — `A/c bal low`, `Remitter CBS down`, `REFER TO ISSUER`. The rest carry
  a mapped code in the bank's dialect. `FailureClass.UNKNOWN` has no entry in either table
  and always yields an empty code, counted as unmapped.
- **Ground truth is never exposed.** `DebitOutcome.true_failure_class` is available to the
  evidence layer for the failure-class breakdown. The agent sees only `raw_code` and
  `narration`.

The issuer's parameters are **not** imported from `anvil/domain/taxonomy.py`. That
separation is what makes the calibration report in §7.6 a measurement rather than a
tautology.

`World.effective_ability` applies one further conditioning step: a case whose true class
is `INSUFFICIENT_FUNDS` has its ability multiplied by 0.45 on every subsequent
presentment. Without it a failed debit would be independent of the next one, retrying
immediately would almost always work, and the optimal policy would collapse to "retry soon
and often".

### 4.3 The customer — `anvil/simulator/customer.py`

Two latent variables nothing downstream may read: **ability to pay** and **intent to pay**.
An insufficient-funds decline from a high-intent, low-ability customer and one from a
low-intent, high-ability customer are identical on the wire.

```
P(read) = responsiveness · channel_affinity · language_factor · fatigue(contacts) · quiet_hour_factor
P(act | read) = intent · cause_relevance · purpose_friction · (1 + concession_acceptance)
churn_hazard  = baseline_churn · (1 + 0.28·contacts) · (1 + 0.18·failed_attempts) · (1.60 − intent)
```

Contact fatigue is `1 / (1 + 0.35 · contacts)`. Wrong language costs 38% of read
probability, quiet hours (21:00–08:00 IST) cost 70%, and outreach that misdiagnoses the
cause costs 55% of act probability. Concession acceptance saturates as `r / (r + 0.18)`,
so the planner is rewarded for finding the smallest concession that clears the bar.

### 4.4 What the simulator does not model

This list is the important half of this section.

- **No terminal mandate or instrument failures reach the batch.** `open_cases` sets
  `instrument_expired` only when `instrument_expires_at <= at`, and `build_customer`
  always places card expiry in the future (`now + 10..900 days`). `mandate_revoked`,
  `account_closed` and `mandate_paused` are never set at open time, and
  `Issuer._failure_class` never draws them without the corresponding flag. At the default
  seed the entire at-risk mix is `insufficient_funds` (323), `issuer_technical` (207),
  `limit_exceeded` (29), `risk_declined` (19), `unknown` (16), `auth_required` (14).
  **The taxonomy's terminal classes — precisely the ones the scheduler exists to refuse —
  are absent from the population being measured.**
- **A risk decline is not sticky.** `_TERMINAL_FOR_DEBIT` includes `RISK_DECLINED` for the
  control and baseline arms, and the anvil arm refuses to retry one it recognised. But
  `World._present` does not zero the settle probability for a risk decline, so in this
  world a risk-declined retry *can* settle. Refusing it therefore costs measured recovery,
  while the real reason to refuse — degrading the merchant's issuer risk score — is not
  modelled at all.
- **No cost is charged for burning a mandate's presentment allowance.** The baseline
  spends up to three attempts per case with no penalty beyond the issuer's 4%-per-attempt
  fatigue term.
- **No issuer-relationship cost, no chargeback, no gateway fee, no tax.** The only costs
  in the model are concessions, 25 paise per message, and 3 paise per model
  classification.
- **No customer churn feeds back into recovery.** `CustomerModel.churn_hazard` is computed
  and returned, but neither `run_baseline` nor the anvil arm's channel adapter acts on a
  `churned` state, and `CaseStatus.CHURNED` is never assigned by the batch.
- **No gateway auto-retry.** The control arm measures self-cure only. A merchant who does
  nothing but whose gateway still retries would sit between control and baseline, and is
  not represented.
- **No partial recovery.** A case recovers for its full amount or not at all.
- **No time-varying self-cure.** Control is one Bernoulli draw, not a hazard over the
  month.
- **The horizon is inert.** `--horizon-days` is accepted, passed to `World.__init__` and
  stored as `self.horizon_days`. Nothing reads it. Case length is bounded by the graph's
  own stopping rules and its recursion limit of 80, not by the horizon.
- **Every case fails at the top of the salary cycle.** `open_cases` presents subscription
  *i* at `BATCH_EPOCH + (i % 10) + 4 hours + (i % 3) days`. At the default seed every
  failure lands on 1–4 September IST, in the hours 15:00–00:59 IST, with 127 of 608 at
  IST hour 0. No case is ever presented during business hours, and no case fails in the
  thin part of the month. The payday strategy the scheduler is built around is therefore
  measured only from its least favourable starting point.
- **Approvals are auto-resolved.** `_AutoApproval` returns immediately and any graph
  interrupt is resumed with `approve` (or `succeeded` for an AFA step-up), up to
  `_MAX_INTERRUPT_RESUMES = 8`. An unattended approval is not evidence that a human would
  have approved.

---

## 5. Outcome metrics

`CaseOutcome` (`anvil/simulator/world.py`) is the record; `anvil/evidence/metrics.py`
aggregates it.

### 5.1 Per case

| Field | Definition |
|---|---|
| `recovered` | `recovered_minor > 0`. A boolean. This is the success indicator for every rate and every interval in the report. |
| `at_risk_minor` | The subscription amount that failed. |
| `recovered_minor` | Money that came back. Full amount or zero. |
| `concession_minor` | Discount granted. Zero in every arm at present (§9). |
| `channel_cost_minor` | Message spend. 25 paise per message. |
| `model_cost_minor` | 3 paise per classification (`CLASSIFY_COST_MINOR`), and only under `--with-model`. |
| `attempts` | Debit presentments counted by the arm. The baseline counts an attempt against a terminal case it never presented. |
| `contacts` | Messages sent. |
| `true_failure_class` | Ground truth from the issuer. Used only for the breakdown table. |
| `classified_deterministically` | `True` if the code tables resolved it, `False` if the case was escalated, `None` for non-anvil arms. |
| `predictions` | `(ex-ante probability in bps, settled)` pairs, one per presentment. Feeds calibration. |

### 5.2 Per arm

`ArmResult` sums the above and adds:

- `rate` — an `Interval`: `recovered_count / case_count` with a bootstrap CI.
- `net_recovered` = `recovered − concessions − channel_cost − model_cost`. Checked by
  `conserves_money`, which is asserted over 200 Hypothesis-generated examples in
  `tests/unit/test_evidence.py`.
- `total_cost` = `concessions + channel_cost + model_cost`.
- `by_failure_class` — `{class: (cases, recovered)}`.
- `value_recovered_bps`, `cost_per_recovered_rupee_bps`, `attempts_per_recovery` — defined
  on `ArmResult` but **rendered nowhere**: not in the text report, not in `as_json`, not
  in the API view.

### 5.3 The cost columns are asymmetric

Two facts, both structural:

1. **The anvil arm's channel spend is never recorded.** The graph accumulates
   `channel_cost_minor` in its state (`anvil/graph/nodes/act.py`), and the batch's channel
   adapter returns `cost_minor: 25` per send, but `_run_anvil_async` copies
   `amount_recovered_minor`, `concession_granted_minor`, `attempts_made`, `contacts_made`,
   `model_safety_events` and `model_cost_minor` out of the final state and **not**
   `channel_cost_minor`. In the runs below the anvil arm sent zero messages, so the
   reported zero is also the true value — but the wiring would not report the spend if it
   did.
2. **The baseline is charged for a message the anvil arm never sends.** With the model
   unavailable, and with the model available, the anvil arm's `contacts` is 0 (§9). The
   cost comparison in the report is therefore between an arm that pays 25 paise per case
   and an arm that pays nothing.

---

## 6. The statistics

`anvil/evidence/statistics.py`. Every function is seeded; nothing rounds an inconvenience
away.

### 6.1 Per-arm rate interval — `bootstrap_proportion`

A **percentile bootstrap**. The observed arm is materialised as a list of 1s and 0s,
resampled with replacement `n` times, 4,000 times over (`DEFAULT_RESAMPLES`). The interval
is the 2.5th and 97.5th percentiles of the resampled rates, computed by linear
interpolation between order statistics (`_percentile`, equivalent to the type-7 quantile).
The point estimate is the plug-in `successes / trials`, not the bootstrap mean.

`trials <= 0` returns `Interval(0, 0, 0)` rather than raising.

### 6.2 Difference interval — `bootstrap_difference`

The only comparison the report treats as a test. Both arms are resampled independently
within each of the 4,000 iterations and the difference of the two resampled rates is
recorded; the interval is the 2.5th/97.5th percentiles of those 4,000 differences. The
point estimate is the plug-in difference of observed rates.

The module docstring states why this exists rather than an overlap check on two per-arm
intervals: two intervals can overlap while the difference is clearly non-zero, so the
overlap heuristic reports no effect when there is one, and it can be run backwards to
claim significance at a confidence level nobody chose.
`test_the_difference_interval_is_computed_on_the_difference` pins that case.

### 6.3 Significance — `is_significant`

```python
return not difference.crosses_zero      # crosses_zero: low_bps <= 0 <= high_bps
```

That is the whole rule. There is no p-value threshold, no rounding, no "directionally
positive" branch.

### 6.4 The z-score — `two_proportion_z`

A pooled-variance two-proportion z, computed and printed for every comparison. It is a
**cross-check on the bootstrap and nothing more** — it does not participate in the
significance decision. No continuity correction.

### 6.5 What is not done

Stated explicitly, because absence is easy to miss:

- **No multiple-comparison correction.** Three comparisons are computed at 95% each:
  `baseline vs control`, `anvil vs control`, `anvil vs baseline`. There is no Bonferroni,
  no Holm, no FDR control. Under a global null the family-wise error rate for three
  independent tests at α = 0.05 is roughly 14%, not 5%. Every interval in the report
  should be read as a *marginal* 95% interval.
- **No bias correction or acceleration.** These are percentile intervals, not BCa. For a
  proportion as extreme as the baseline arm's 87.7%, percentile-bootstrap coverage is
  known to be worse than nominal.
- **The two control comparisons share an RNG seed.** `summarise` calls
  `compare(result, control, seed=seed)` for each non-control arm and
  `compare(anvil, baseline, seed=seed + 1)` for the head-to-head, so the
  `baseline vs control` and `anvil vs control` bootstraps run off the same random stream.
- **No interval on any money figure.** `net_difference` is a plain subtraction of two arm
  totals.
- **`net_difference` is not normalised by arm size.** Under an unequal split it is
  dominated by how many cases each arm received. At `--split production` the report shows
  `anvil vs baseline` at −22.1 points on recovery rate *and* +₹49,023.20 on net money,
  because the anvil arm held 504 cases against the baseline's 53. The rate difference is
  the comparison; the money line is a total.
- **No inference on the failure-class breakdown.** That table is raw counts, with no
  intervals and no correction, over cells as small as 4 cases.
- **No sequential-testing control.** Nothing stops a reader from running seeds until one
  is flattering. The seed is printed in the report and in the reproduce line precisely so
  that a selected seed is visible.

### 6.6 Assumptions

- Cases are independent within an arm. In the simulator they are: each customer's
  substreams are keyed by customer id and message key, and no case reads another's state.
- The recovery indicator is Bernoulli and identically distributed within an arm. It is
  not — the failure-class mix differs between arms by sampling alone, and recovery
  probability depends strongly on class. The bootstrap over the arm as a whole absorbs
  that variation rather than conditioning on it. There is no stratified or
  covariate-adjusted estimator.
- Assignment is independent of potential outcomes. It is, by construction: the arm is a
  hash of `(seed, case_id)` and the case id is a hash of `(customer_id, seed)`, fixed
  before any presentment.

---

## 7. Reproducing a run

No database, no network, no credentials. The batch runs entirely offline.

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

make batch                                  # seed 20260902, size 3000, even split, model unavailable
make batch SEED=20260902 SIZE=3000          # the same run, stated explicitly
make batch-with-model SEED=20260902 SIZE=3000
```

`make batch` shells out to the module, and the module takes more flags than the Makefile
exposes:

```bash
.venv/bin/python -m anvil.evidence.run_batch \
    --seed 20260902 \
    --size 3000 \
    --split even \
    --with-model \
    --horizon-days 30 \
    --json /tmp/batch.json
```

| Flag | Default | Effect |
|---|---|---|
| `--seed` | `20260902` | Seeds the population, issuer, customer model, assignment and bootstrap. Must be positive. |
| `--size` | `3000` | Subscriptions in the book, not cases in the experiment. |
| `--split` | `even` | `even` = 33/33/33, `production` = 10/10/80. |
| `--with-model` | off | Models the LLM classifier as available (§7.2). |
| `--horizon-days` | `30` | Accepted, stored, **never read**. |
| `--json` | none | Also writes the `as_json` payload to this path. |

The same experiment is served by the API at `GET /api/batch?seed=&size=&with_model=`
(`anvil/api/routers/evidence.py`), which always uses `EVEN_SPLIT`, caps `size` at 4,000,
and caches per `(seed, size, with_model)` so a dashboard cannot be reloaded until the
number is agreeable.

### 7.1 The output is noisy, on stdout

`run_batch.main` calls `structlog.configure(...)` to filter below ERROR, but
`anvil.core.logging.get_logger()` binds its proxy at import time, so the configuration
never reaches the already-bound loggers. At `--size 3000` the run prints **879 warning
lines before the 87-line report, on stdout, not stderr**: 611 `planner_unavailable`, 216
`diagnosis_unavailable`, 52 `classifier_model_unavailable`. They are expected — they are
the degradation path being exercised once per case — but `make batch > file` captures
them along with the report.

Use `--json` for a machine-readable result, or slice the report from the first `=====`
rule.

### 7.2 What `--with-model` actually does

It does **not** call Anthropic. It swaps `_FallbackModel` for `_ClassifyingModel`
(`anvil/simulator/world.py`), a stand-in that:

- answers **only** `diagnose(purpose="classification")`; `plan` and `compose` still raise,
- returns the true failure class with probability `CLASSIFIER_ACCURACY = 0.88`, and a
  plausible wrong one otherwise, so the measured benefit includes the cost of being wrong,
- charges 3 paise per call.

So `--with-model` isolates exactly one thing: what it is worth to understand a reason code
nobody wrote a rule for. It does not measure planning or composition, and no run of this
batch has ever involved a language model.

### 7.3 Reproducibility caveats

- The `Reproduce exactly` footer prints only `make batch SEED=… SIZE=…`. It does not
  include `--split` or `--with-model`, so a `--with-model` run prints a reproduce line
  that will not reproduce it. Use `make batch-with-model`.
- Per-arm CI bounds vary by ±1 bps across processes unless `PYTHONHASHSEED` is fixed
  (§2.5). Everything else is byte-identical.

---

## 8. Reading the report

Field by field, against the run at `--seed 20260902 --size 3000` (even split, model
unavailable).

### 8.1 Header

```
seed 20260902   population 3,000 subscriptions   608 at-risk cases
money at risk: ₹1,02,940.20
language model: UNAVAILABLE - every case ran on the deterministic fallback
```

`population` is `--size`. `at-risk cases` is the experiment's actual sample.
`money at risk` is the sum of `at_risk` across all three arms. The model line reads
`classification available` under `--with-model`.

### 8.2 PER-ARM OUTCOMES

```
  arm                                     n    rate            95% CI     recovered
  control (no intervention)             181   19.9%      [14.4, 26.0]     ₹5,415.30
  baseline (fixed day 1/3/5 dunning)    211   87.7%      [82.9, 91.9]    ₹31,947.30
  anvil (the agent)                     216   63.4%      [56.9, 69.9]    ₹23,240.25
```

`n` is cases assigned to the arm. `rate` is `recovered_count / n`. The CI is the percentile
bootstrap of §6.1 — **the per-arm interval, which is not the test.** `recovered` is gross
money.

### 8.3 NET OF WHAT IT COST

```
  arm                                         gross        cost          net attempts
  baseline (fixed day 1/3/5 dunning)     ₹31,947.30      ₹52.75   ₹31,894.55      325
  anvil (the agent)                      ₹23,240.25       ₹0.00   ₹23,240.25      348
```

`cost` is `concessions + channel + model`. The baseline's ₹52.75 is 211 messages at 25
paise. The anvil arm's ₹0.00 is real for this run and structurally unreliable in general
(§5.3). `attempts` counts presentments, including the baseline's counted-but-not-presented
attempts against terminal cases.

### 8.4 LIFT, WITH ITS UNCERTAINTY

```
  anvil vs baseline
    recovery rate difference   -24.2%  [-32.2, -16.3]   (95% bootstrap)
    net money difference       -₹8,654.30
    STATISTICALLY SIGNIFICANT  the interval excludes zero (z = -5.82)
```

- `recovery rate difference` — the plug-in difference in percentage points, with the
  bootstrap interval **on the difference**. Never printed without it; a unit test enforces
  that the line contains brackets.
- `net money difference` — `treatment.net_recovered − against.net_recovered`. An
  unnormalised total (§6.5).
- The verdict line is one of three, and always states the conclusion in words:
  - `STATISTICALLY SIGNIFICANT` — the interval excludes zero.
  - `NOT SIGNIFICANT, AND UNDERPOWERED` — includes zero, and the point difference is
    smaller than the MDE. Names the MDE and says a larger batch is needed.
  - `NOT SIGNIFICANT` — includes zero, and the batch could have detected an effect this
    size. States that the batch provides no evidence of a difference.
- If there is no control arm, the section prints `No control arm in this batch, so no lift
  can be reported` and nothing else. `compare` raises `EmptyControlArm` rather than
  reporting a lift against an empty arm.

### 8.5 WHERE THE RECOVERY CAME FROM

Anvil against baseline, by **true** failure class, sorted by anvil case count. Raw counts,
no intervals. A class the anvil arm never saw is omitted; a class the baseline never saw
shows `-`. Cells go down to 4 cases.

### 8.6 WHAT THE MODEL DID

```
  164/216 failures (76%) were classified by the code tables with no model call.
  52 were escalated because no table recognised the reason string.
  153 of 608 cases carried a reason code no table recognises.
  0 proposed action(s) were refused before execution for falling outside the closed action space.
```

Read the second line precisely. `classified_by_model` counts cases where
`classified_deterministically` is `False`, which means **the code tables failed and the
case was escalated** — not that a model answered. Without `--with-model` those 52 cases
were classified `UNKNOWN` by the fallback. The two lines are byte-identical in the
fallback and `--with-model` runs.

Line three counts cases whose *opening* failure carried an unmapped code, across all three
arms. Line four is the model-safety counter: proposals refused for falling outside the
closed action space, surfaced as a first-class metric rather than hidden.

### 8.7 IS THE SCHEDULER HONEST?

`anvil/risk/calibration.py`, over every `(predicted probability, settled)` pair the batch
collected — 655 presentments in this run, from the baseline and anvil arms.

```
  Systematically over-confident by 9.7 points across 655 attempts (expected calibration
  error 10.0%). The retry curves need re-fitting against observed outcomes.

        band       n   predicted    observed       gap
      60-70%      97       64.1%       45.4%    +18.7%
      90-100%     32       93.3%       93.8%     -0.4%

  Brier score 0.2225   expected calibration error 10.0%
```

- **Reliability table** — ten equal-width probability bands, empty bands omitted (an
  absence of evidence is not a zero gap). `gap = predicted − observed`; positive means
  over-confident.
- **Brier score** — mean squared error on probabilities, rendered as a 0–1 decimal.
- **Expected calibration error** — the bucket-weighted mean absolute gap.
- **Verdict** — "well calibrated" only when ECE ≤ 5 points. Below 100 predictions it
  refuses to assess at all and says so.

This section is the report auditing its own scheduler, and at the default seed **it fails**:
the retry curves in `anvil/domain/taxonomy.py` are hand-written priors, and the table says
they are systematically optimistic in this world.

### 8.8 WHAT THIS RUN DOES NOT SHOW

Generated by `report._limitations`, ordered by how much they should worry the reader, and
always present. Two entries are conditional:

- The model-unavailable note appears only without `--with-model`.
- Two notes about the baseline appear only when the baseline's rate exceeds the anvil
  arm's — including the observation that the baseline pays no penalty here for burning a
  mandate's presentment allowance or damaging an issuer risk score.

The unconditional three: approvals were auto-resolved; outcomes come from a seeded
simulator; the control arm measures self-cure only.

---

## 9. Threats to validity, and what this evidence does not prove

### 9.1 Simulated data is not market evidence

Every number in the report is generated by `anvil/simulator/`, whose parameters were chosen
by the author of `anvil/simulator/`. The issuer's parameters are deliberately not imported
from the scheduler's retry curves, and the unit tests bound its behaviour against publicly
cited ranges — a healthy debit clears at 85–96%, the overnight window is at least twice as
bad as midday, the failure mix is at least 60% recoverable causes. Those checks make the
simulator *internally defensible*. They do not make it *real*. No claim in this document
transfers to a merchant's book.

One concrete divergence: `README.md` states that real subscription books see **6–12%** of
debit attempts fail. This simulator produces **20.3%** at the default seed, and its unit
test only requires 5–25%. The at-risk population is roughly twice as large as the stated
real-world range, which inflates every absolute money figure in the report.

### 9.2 The anvil arm never contacts anyone

In both available configurations — with and without `--with-model` — the anvil arm's
`contacts` is **0** and its `concessions` are **₹0.00**. The reason is structural: only
classification is modelled, `plan` and `compose` always raise, and `_fallback_plan`
proposes a retry when the class is retryable, an instrument-update request when it is
terminal for debit, and a human hand-off otherwise. Because the at-risk mix contains no
terminal instrument or mandate classes (§4.4), the instrument-update branch never fires.

**So the batch measures the retry scheduler and almost nothing else.** The customer model,
contact fatigue, concession sizing, churn pressure, the consent gate, frequency caps and
the entire outreach half of the product are not exercised by the arm they are meant to
distinguish. The one message any customer receives in this experiment is the baseline's.

### 9.3 The agent loses to naive dunning in this world

At the default seed the head-to-head is `anvil vs baseline`: **−24.2 points
[−32.2, −16.3], z = −5.82, significant.** The report says so in the body and again in the
limitations. Three reasons the code documents, none of which are excuses:

1. The retry curves are hand-written priors, not fitted to this issuer, and the
   calibration table in §8.7 measures them as systematically over-confident by 9.7 points.
2. The baseline pays no cost here for burning three presentments per case or for
   retrying a decline no one should retry, because neither cost is modelled.
3. Anvil refuses to retry a recognised risk decline. In this simulator that refusal is
   pure loss, because the issuer will happily settle a risk-declined retry and the real
   penalty for making one is not represented.

The honest reading is that this batch measures a scheduler running on unfitted priors
against a world that does not price the harms the scheduler exists to avoid. That is a
statement about the experiment as much as about the agent.

### 9.4 Enabling the classifier made it worse in this run

`--with-model` moves the anvil arm from **63.4%** to **61.6%** and adds ₹1.56 of model
cost and 32 attempts. The report computes **no interval for a difference between two runs**,
so this 1.9-point gap has no uncertainty attached to it and must not be read as evidence
that the classifier hurts. It is one point difference between two seeded runs. Measuring
the classifier's contribution properly would need it to be an arm, not a flag.

### 9.5 Structural threats

| Threat | Status |
|---|---|
| Every case fails on 1–4 September IST, evening and overnight hours only | Real, unaddressed. The scheduler's payday strategy is measured only from the top of the salary cycle. |
| Multiple comparisons at 95% each, uncorrected | Real. Family-wise error ≈ 14% under a global null. |
| Percentile bootstrap on a rate of 87.7% | Coverage below nominal at that extreme. |
| Terminal failure classes absent from the population | Real. Removes most of the taxonomy from the measurement. |
| Auto-approved interrupts | Real, disclosed in the report. Up to 8 resumes per case. |
| Anvil channel spend not wired into the outcome | Real. Latent today because contacts are zero. |
| Realised-vs-requested split drift is never reported | `RealisedSplit` and `realised_split` exist and are tested, but the batch never calls them and the report never prints arm drift. |
| Assignment audit never run | `assignment.verify()` is written for an auditor and is not exported from `anvil.evidence`, not called by the batch, and has no test. |
| One seed drives world and assignment | Assignment noise cannot be isolated (§2.6). |
| `--horizon-days` inert | Accepted and ignored. |

### 9.6 What would make this evidence

In rough order of how much each would buy:

1. Production or replayed traffic in place of the simulator.
2. Outreach and concessions exercised in the anvil arm, so the comparison covers the
   product rather than one component.
3. Retry curves fitted to observed outcomes via `anvil/risk/calibration.py`, rather than
   hand-written priors, with the calibration report as the acceptance test.
4. Failure timing spread across the month, and across the hours of the day.
5. A pre-registered primary comparison and a correction for the rest.
6. Repeated seeds, so between-run variance is measured rather than assumed away.

---

## 10. Where the numbers come from

| File | Responsibility |
|---|---|
| `anvil/evidence/run_batch.py` | CLI, `BATCH_EPOCH`, wiring only |
| `anvil/evidence/assignment.py` | Hash assignment, splits, drift, `verify` |
| `anvil/evidence/metrics.py` | `CaseOutcome` → `ArmResult` → `BatchSummary`, `as_json` |
| `anvil/evidence/statistics.py` | Bootstraps, z, significance, MDE |
| `anvil/evidence/report.py` | The printed report and `_limitations` |
| `anvil/risk/calibration.py` | Reliability table, Brier, ECE, verdict |
| `anvil/simulator/world.py` | The three arms, port adapters, `open_cases` |
| `anvil/simulator/issuer.py` | Settlement hazard model, reason strings |
| `anvil/simulator/population.py` | The book |
| `anvil/simulator/customer.py` | Read, act, concession and churn probabilities |
| `anvil/simulator/rng.py` | Substreams, Bernoulli, weighted choice, skew |
| `anvil/core/clock.py` | `FrozenClock`, IST helpers |
| `anvil/core/ids.py` | `deterministic_id` |
| `tests/unit/test_evidence.py` | Assignment, statistics, metrics, report guarantees |
| `tests/unit/test_simulator.py` | Reproducibility and distribution checks |

Run them with:

```bash
.venv/bin/python -m pytest tests/unit/test_evidence.py tests/unit/test_simulator.py -q
```
