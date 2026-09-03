# Testing

**203 tests. No database, no network, no model, no API key.**

The whole suite runs in one process against hand-written doubles. That is not a
convenience — it is the reason the failure paths are tested at all. A gateway
that times out, a model that is unreachable, an issuer that declines: none of
these can be *waited for*, so all of them are *constructed*.

```bash
.venv/bin/python -m pytest tests/unit -q        # the whole suite
.venv/bin/python -m pytest -m invariant -q      # only the financial invariants
```

The count is verified, not carried forward: `pytest --collect-only` reports 203,
and so do commit `238bcd9` and the README. Re-verify it whenever you quote it —

```bash
.venv/bin/python -m pytest tests/unit --collect-only 2>&1 | tail -1
```

---

## The shape of the suite

It is not a pyramid. It is one wide layer.

| Tier | Files | Tests | Needs a database |
|---|---|---:|---|
| `tests/unit/` | 6 | 203 | No |
| `tests/integration/` | none — package only | 0 | Would |
| `tests/e2e/` | none — package only | 0 | Would |

`tests/integration/` and `tests/e2e/` contain nothing but an empty
`__init__.py`. The packages exist and the `integration` and `e2e` markers are
registered in `pyproject.toml`, but **no test carries either marker yet**. The
Makefile's `test-all` and the `db-up`/`migrate` targets it depends on are wired
for a tier that has not been written.

The reason the unit tier is this wide is `anvil/graph/ports.py`. The
orchestrator imports none of the modules it drives; it declares twelve narrow
Protocols and the composition root supplies implementations. So
`tests/unit/test_graph.py` exercises the *complete* recovery state machine —
both durable interrupts, every degradation path, the closure classifier —
without a database, and it belongs in `unit/` because it needs nothing outside
the process. What would justify a genuine integration tier is the part the
Protocols abstract away: the `SELECT … FOR UPDATE` budget lock, the append-only
Postgres triggers, LangGraph's Postgres checkpointer, and the webhook dedupe
constraint. Those are asserted today at the arithmetic layer, not against a
live server.

---

## What each file covers

### `tests/unit/test_ledger.py` — 25 tests, 8 of them `invariant`

The four ledger invariants from `docs/ARCHITECTURE.md` §6, written as
properties rather than examples. "This particular posting balances" is a much
weaker claim than "no posting this module can construct fails to balance".

- **Invariant 2, every transaction balances.** `ALL_BUILDERS` lists all seven
  posting builders; one Hypothesis property runs every builder at every amount
  and asserts `imbalance_minor == 0`. The file's own docstring calls this the
  single most important test in the repository. A second property asserts
  `validate` accepts *exactly* the balanced arrangements and nothing else.
- **Invariant 3, integer minor units.** Entries must be strictly positive —
  the side is carried by `EntryDirection`, never by sign. `Money.from_major`
  and `Money.scale` refuse floats at the type level.
- **Invariant 5, idempotency.** A key is a pure function of intent, so a
  network retry collapses instead of paying twice. Different intents, cases and
  amounts get different keys.
- **Invariant 8, the concession budget.** Headroom subtracts both settled and
  held; two concurrent requests cannot jointly overspend; a property asserts
  headroom is an absolute ceiling whatever the caps say; each cap reports
  itself as the limiting one so an operator can see which ceiling stopped the
  agent.
- **Reversal.** A mirrored draft nets every touched account to zero and never
  edits the original.
- **Economic correctness.** Settlement moves receivable into cash. A
  concession costs revenue rather than cash and consumes its earmark, in four
  legs, none of which may be collapsed. Recognition then write-off leaves the
  receivable where it started. A transaction may not span merchants. A
  single-entry transaction is refused.

### `tests/unit/test_policy.py` — 41 tests

The evaluator's semantics get exhaustive coverage because a regression in any
one would be invisible in ordinary use and catastrophic in an audit.

- **The four semantics.** No match denies. An empty bundle denies everything.
  First deny wins and stops evaluation — a later ALLOW cannot rescue it, and
  the trace proves the later rule was never considered. Approval is sticky and
  cannot be downgraded. The tightest cap wins, caps accumulate, and a ceiling
  above the request leaves the request alone.
- **Failing loud.** A malformed expression or a rule naming an unknown fact
  raises `MalformedExpression`. "I could not check this constraint" must never
  read as "it passed".
- **The default bundle.** Withdrawn and absent consent block outreach. Quiet
  hours block ordinary outreach, and step-up authentication is the only
  exemption. Frequency caps, never-retry classes, unauthorised money movement,
  review-first escalation, both concession ceilings, and the guarantee that
  stopping is never blocked.
- **Totality.** A Hypothesis property runs the default bundle over every
  `ActionType` at arbitrary amounts, hours, contact counts and tenures and
  asserts it always produces a decision and never raises.
- **Hashing.** `bundle_hash` ignores insertion order, changes when behaviour
  changes, and changes when a description stops describing its rule.
- **The compiler.** A proposal that drops, renames or rewrites a regulatory
  rule is refused; so are unknown facts, the reserved priority band, an empty
  description, and a CAP with no ceiling. Every problem is reported at once.
  Two async tests use a local `Stub` for the model port — one asserts the model
  is **not called at all** for empty prose.

### `tests/unit/test_risk.py` — 40 tests

- **The scheduler**, and this is the file's centre of gravity. A balance
  failure mid-cycle must be willing to wait nearly a fortnight to reach a
  payday; a technical decline retries within hours; a limit failure waits for
  the reset boundary. Terminal classes are never scheduled and the refusal
  carries a reason a human can read. The minimum inter-presentment gap, mandate
  expiry and the mandate's attempt allowance are all hard boundaries.
- **The dynamic program behaves like one.** Value is monotone in the attempt
  budget, never exceeds the amount at risk, and scales linearly with it so the
  chosen hour does not depend on ticket size. The argmax agrees with the
  ranking, or the console shows a lie. A Hypothesis property asserts the
  scheduler is total.
- **Classification.** Recognised codes resolve with no model; unrecognised
  free text escalates rather than guessing.
- **Scoring.** A property keeps all three scores in `[0, 1000]`. More contacts
  never lower churn risk. Prior recoveries never lower recovery likelihood. An
  unknown customer sits at the midpoint rather than being treated as a bad
  payer.
- **Calibration.** Perfect calibration is recognised, over-confidence is named
  as over-confidence, the Brier score matches a hand computation, a small
  sample refuses to draw a conclusion, and an empty bucket is omitted rather
  than reported as a zero gap.
- **Detection.** Failed debits, degrading subscriptions caught before they
  fail, expiring mandates and instruments, urgency ordering, skipping
  subscriptions already being worked, and counting money at risk once per
  subscription rather than once per signal.

### `tests/unit/test_graph.py` — 39 tests

The full recovery graph over twelve hand-written doubles plus a `FrozenClock`.

- **Happy path.** A technical decline is retried and recovered; the receivable
  is recognised *before* anything is recovered, because a later write-off must
  reduce a real asset; a recovered case writes nothing off.
- **Invariants 6 and 7, checked structurally.** The audit trail must contain
  `authorisation_checked` and `policy_evaluated`, both at indices *before*
  `action_executed`. A policy denial or an authorisation denial leaves
  `gateway.keys` empty.
- **The model is bounded.** An out-of-bounds action type, a concession with no
  amount and a negative amount are each refused and counted as
  `model_safety_events`. A case with no usable plan escalates rather than
  stalling.
- **Degradation.** With `StubModel(available=False)` every model call raises
  and recovery still completes and settles. The fallback plan never offers a
  concession, because pricing one was exactly the model's job. An unavailable
  classifier falls back to `UNKNOWN`. A deterministically classified case never
  calls the model to classify.
- **The unknown gateway outcome.** A timeout posts no settlement, records zero
  recovered, parks the case in `PENDING_RECONCILIATION`, and writes nothing
  off. Idempotency keys are stable per logical action and unique across
  actions.
- **Both durable interrupts.** Approval pauses the graph with the queue item
  already created and nothing executed; resuming with `approve` executes,
  `reject` does not, and `edit` amends the action that actually runs rather
  than merely suggesting an amendment. AFA step-up pauses the same way and a
  failed step-up does not execute.
- **Resting states.** Across three gateway outcomes and two channel outcomes,
  the graph always lands in a terminal status or `PENDING_RECONCILIATION` —
  never an open loop, never a status nobody chose.
- **Closure classification**, called directly on `decide_closure`: a revoked
  mandate is churn rather than failure, an expired card with no recovery is
  unrecoverable rather than churn, a partial recovery counts as recovered, and
  a parametrised test asserts closure is total over every `FailureClass`.

### `tests/unit/test_simulator.py` — 31 tests

- **Reproducibility**, which is the claim the submission makes in writing. The
  same seed builds the same population by fingerprint, the same at-risk set,
  the same issuer outcome and the same whole batch. A different seed builds a
  different population.
- **Issuer calibration**, the second line of defence against a simulator that
  is reproducible but wrong. A healthy debit clears between 85% and 96%. The
  overnight maintenance window is material. The salary cycle moves a stretched
  customer and barely moves a comfortable one. Terminal conditions settle with
  probability exactly zero. Repeat presentments get harder. A weak bank is
  weak every time.
- **Reason codes.** Roughly a fifth are unmapped, and an unmapped code must
  genuinely defeat `classify_code` rather than quietly resolving. Mapped codes
  do resolve.
- **The population is a plausible book.** A realistic monthly failure rate,
  most subscribers able to afford their subscription, a mandate mix that
  includes Reserve Pay and delegated authority so the step-up path runs, a plan
  ladder with somewhere to downgrade to, and no placeholder names.
- **Arm definitions.** Control never attempts anything but still recovers
  something, because self-cure is real. The baseline retries blindly including
  terminal cases. Anvil never retries a decline it *recognised* as a risk
  decline, and an unrecognised one costs at most one attempt.

### `tests/unit/test_evidence.py` — 27 tests

Each of these guards a specific way an experiment write-up can mislead.

- **Assignment** is deterministic and recomputable, actually depends on the
  seed, converges on the requested split within 250 bps over 6000 cases, and
  refuses a split that does not sum to 10000 — an unassigned remainder would
  silently drop cases out of the experiment. The production split holds back a
  real control.
- **Statistics.** The interval is computed on the *difference*, and one test
  deliberately constructs two arms whose own intervals overlap while the
  difference is non-zero, because eyeballing overlap is a different, weaker and
  wrongly-calibrated test. A clear effect is significant, a null effect is not,
  and a negative effect is reported as negative — the agent losing must be
  reportable, not merely representable. A z-test cross-checks direction.
  Minimum detectable effect shrinks with sample size. An empty arm yields an
  empty interval rather than a crash.
- **Metrics.** Net is gross less every cost, and a Hypothesis property asserts
  the arithmetic conserves money. A lift against an empty control raises
  `EmptyControlArm`. Recovery is broken out by failure class so a headline
  number cannot hide one class doing all the work.
- **The report** states significance in words, never shows a lift without an
  interval, says when the baseline wins, always carries a limitations section,
  discloses whether the model was available, and is byte-stable for a fixed
  input.

---

## Markers

Three are registered in `pyproject.toml`, and `addopts` includes
`--strict-markers`, so an unregistered marker is an error rather than a silent
no-op.

| Marker | Declared as | Used by |
|---|---|---|
| `invariant` | enforces a financial invariant from ARCHITECTURE.md section 6 | 8 tests, all in `test_ledger.py` |
| `integration` | requires a live Postgres | nothing yet |
| `e2e` | full stack end-to-end | nothing yet |

```bash
.venv/bin/python -m pytest -m invariant -q                 # 8 selected, 195 deselected
.venv/bin/python -m pytest -m "not invariant" -q
.venv/bin/python -m pytest tests/unit/test_graph.py -q
.venv/bin/python -m pytest -k "idempotency or reversal" -q
```

`make invariants` is the same selection. The eight tests it runs are the ones
that fail the build if money can be created or destroyed:

```text
test_every_builder_produces_a_balanced_transaction        invariant 2
test_unbalanced_transaction_is_refused                    invariant 2
test_validate_accepts_exactly_the_balanced_arrangements   invariant 2
test_entries_must_be_strictly_positive                    invariant 3
test_reversal_nets_the_original_to_zero                   reversal
test_idempotency_key_depends_only_on_intent               invariant 5
test_headroom_subtracts_both_settled_and_held             invariant 8
test_two_concurrent_concessions_cannot_jointly_overspend  invariant 8
```

Mark a new test `invariant` only when failing it means money moved wrongly —
not merely that a module misbehaved. The selection is meant to stay small
enough to read.

---

## pytest configuration

All of it lives in `[tool.pytest.ini_options]` in `pyproject.toml`. There is no
`pytest.ini` and no `setup.cfg`.

```toml
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-q --strict-markers"
```

`asyncio_mode = "auto"` matters when writing tests: an `async def test_…`
needs **no** `@pytest.mark.asyncio` decorator and no event-loop fixture. Roughly
half of `test_graph.py` and two tests in `test_policy.py` rely on this.

---

## Fixtures

There is exactly one, in `tests/conftest.py`:

```python
@pytest.fixture(autouse=True, scope="session")
def _quiet_logs() -> None:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))
```

`_quiet_logs` raises the structlog threshold to ERROR for the whole session.
The degradation warnings the graph emits when the model is unavailable are
expected behaviour, not noise to be fixed — but a batch test produces thousands
of them and they bury the actual failures. It is silenced here rather than in
the modules so production logging is untouched.

Everything else is a plain function. `test_graph.py` uses `make_deps()`,
`make_state()` and `run()`; `test_ledger.py` uses `ctx()`, `position()` and
`request()`; `test_policy.py` uses `rule()`, `bundle()`, `facts()`,
`outreach()` and `proposal()`. Each takes `**overrides` and merges them over a
base dict, so a test names only the thing it is varying. That is the house
style: builders over fixtures, because a fixture that takes parameters is
harder to read than a function that does.

---

## How determinism is achieved

Four mechanisms, none of them optional.

**The clock is injected.** Nothing in Anvil calls `datetime.now()` directly.
`anvil/core/clock.py` declares a `Clock` Protocol with two implementations:
`SystemClock` for production and `FrozenClock` for tests and the simulator.
`FrozenClock` exposes `set`, `advance`, `advance_hours` and `advance_days`, so
a test can place the system at any instant and the batch can run thirty days of
recovery in a second of wall time. Naive datetimes are refused outright by
`_require_aware`, and ruff's `DTZ` rules enforce the same thing statically.
`IST` is defined here because quiet hours, salary cycles and issuer maintenance
windows are all IST concepts, computed in IST and stored in UTC.

**Instants are module constants.** Every test file fixes its own reference
time and every case is derived from it, so nothing depends on the day the suite
runs:

```python
AT                = dt.datetime(2026, 9, 2,  11, 30, tzinfo=dt.UTC)   # test_ledger
NOW               = dt.datetime(2026, 9, 18,  6,  0, tzinfo=dt.UTC)   # test_graph
MID_CYCLE_FAILURE = dt.datetime(2026, 9, 18,  6,  0, tzinfo=dt.UTC)   # test_risk
NOW               = dt.datetime(2026, 9, 1,   6,  0, tzinfo=dt.UTC)   # test_simulator
BATCH_EPOCH       = dt.datetime(2026, 9, 1,   6,  0, tzinfo=dt.UTC)   # run_batch
```

`MID_CYCLE_FAILURE` is chosen, not arbitrary: 18 September is mid-cycle, when
balances are thinnest, which is the hardest starting point for the scheduler
and therefore the most informative one.

**Randomness is seeded and substreamed.** `anvil/simulator/rng.py` gives every
logical subject its own generator keyed by a label:

```python
substream(seed, "attempt", attempt_id)   # blake2b(seed ‖ labels) -> random.Random
```

A single shared `random.Random` would make every outcome depend on the *order*
draws happened to occur in, so adding one message would change every subsequent
coin flip. Keying on `(seed, label)` means the world can process events in any
order and the tests can query the issuer out of band without perturbing
anything. The module also refuses transcendental floats: `log`, `exp` and
`gauss` route through libm and are not bit-reproducible across platforms, so
the skewed distributions are built from integer draws and exact `Decimal`
ratios. `bernoulli` compares an integer draw against a probability scaled to
parts per million — no float rounding anywhere in the decision.

The seed is `20260902` everywhere: `ANVIL_SEED` in `.env.example`,
`Settings.seed` in `anvil/core/config.py`, `SEED` in `test_simulator.py` and
`test_evidence.py`, and `--seed` in `make batch`.

**Statistics take an explicit seed.** Every function in
`anvil/evidence/statistics.py` accepts `seed=`, so an interval computed on one
machine is the interval computed on another —
`bootstrap_proportion(300, 1000, seed=7)` is asserted equal to itself across
calls. Arm assignment is a pure function of `(batch_seed, case_id)` hashed into
10000 integer buckets, with no floating-point threshold anywhere on the path,
and it returns the full digest as a receipt so anyone who suspects the control
arm was pruned can recompute every assignment from the seed and the case ids
alone.

---

## The ports, and the doubles that stand in for them

`anvil/graph/ports.py` declares twelve `@runtime_checkable` Protocols —
`ClassifierPort`, `SchedulerPort`, `ScoringPort`, `ModelPort`,
`AuthorisationPort`, `PolicyPort`, `ApprovalPort`, `LedgerPort`,
`GatewayPort`, `ChannelPort`, `AuditPort`, `CasePort` — and
`anvil/graph/deps.py` holds them in a frozen `Deps` dataclass alongside the
`Clock` and the `allowed_actions` tuple. `build_graph(deps, checkpointer=…)`
closes over it.

The slice each port exposes *is* the contract. `LedgerPort` is the one to read
first: it has no `post` and no way to construct an arbitrary entry. The
orchestrator can record four economic events and take, release or settle a
reservation. Nothing else. A bug in a node cannot invent a posting the chart of
accounts never anticipated.

`test_graph.py` supplies a double for all twelve, each parameterised by the
failure it is there to produce:

| Double | Constructor knob | Produces |
|---|---|---|
| `StubClassifier` | `resolved=False` | an unresolvable failure code |
| `StubScheduler` | `should_retry=False` | a refusal with a reason |
| `StubModel` | `available=False` | every model call raises |
| `StubModel` | `steps=[…]` | an arbitrary plan, valid or not |
| `StubAuthorisation` | `decision=…` | denied, or `requires_step_up` |
| `StubPolicy` | `effect=…` | deny, or `require_approval` |
| `StubLedger` | `budget_available=False` | `BudgetExhausted` |
| `StubGateway` | `outcome="unknown"` | a timeout with no known outcome |
| `StubChannels` | `sent=False` | suppression by frequency cap |

Each also records what it was asked to do, and the assertions are made against
those records: `StubGateway.keys`, `StubLedger.postings`, `StubAudit.types()`,
`StubApprovals.requested`, `StubChannels.dispatched`. `gateway.keys == []` is
how "nothing executed" is asserted, and it is a stronger claim than checking a
status field.

`make_deps(**overrides)` assembles a working default set, so a test names only
the double it is varying:

```python
deps = make_deps(model=StubModel(available=False), gateway=StubGateway("settled"))
final = await run(deps, make_state())
```

`run()` builds the graph with an in-memory `MemorySaver` checkpointer and a
recursion limit of 60. Tests that exercise an interrupt call `graph.ainvoke`
twice against the same `thread_id` — once to reach the pause, once with a
`Command(resume=…)`.

---

## How the LLM is faked

There is no network call in the suite, no recorded cassette, and no VCR-style
replay. `respx` and `freezegun` are declared in the `dev` extra but are not
used anywhere yet, and `anvil/llm/fixtures/` is an empty directory. The model
is faked three ways, all of them plain classes satisfying `ModelPort`.

**1. `StubModel` in `tests/unit/test_graph.py`.** Implements `diagnose`,
`plan`, `compose` and the `cost_minor` property. Its `_guard` raises
`RuntimeError("anthropic api unavailable")` when `available=False`, which is
what drives the graph down its documented degradation path — the same path
that runs in production when Anthropic is unreachable. The `steps` argument
hands the planner an arbitrary plan, which is how the out-of-bounds proposals
get tested: an invented action type, a concession with no amount, a negative
amount.

**2. `_FallbackModel` in `anvil/simulator/world.py`.** Every method raises.
This is the model port the reproducible batch uses by default, and the choice
is deliberate: every number `make batch` produces is a **floor**, achieved with
the language model contributing nothing, reproducible on any machine with no
API key.

**3. `_ClassifyingModel` in `anvil/simulator/world.py`,** used by
`make batch-with-model`. It models one job only — resolving the free-text
reason strings the deterministic tables cannot, which is precisely what
`anvil/risk/classifier.py` escalates. Planning and composition still raise. It
is deliberately not an oracle: `CLASSIFIER_ACCURACY = Decimal("0.88")`, and the
other 12% of the time it returns a plausible wrong class drawn from a seeded
substream, so the measured benefit includes the cost of the model being wrong.
Each call charges `CLASSIFY_COST_MINOR = 3` paise to the case. Isolating
classification is what makes the difference between the two batch arms mean one
thing: what it is worth to understand a reason code nobody wrote a rule for.

`test_policy.py` also defines two throwaway `Stub` classes with a single
`compile_policy` method, for the natural-language policy compiler. One of them
raises `AssertionError` in its body — the assertion being that the model must
not be called at all for empty prose.

---

## Property-based tests

Hypothesis is used in four files: `test_ledger.py`, `test_policy.py`,
`test_risk.py` and `test_evidence.py`. The pattern throughout is
`@given(...)` plus `@settings(max_examples=N, deadline=None)`. The deadline is
disabled because several of these properties do real work per example and a
timing-based flake in an invariant test would be worse than a slow one.

```python
money = st.builds(Money, st.integers(min_value=1, max_value=50_000_000), st.just(Currency.INR))

@pytest.mark.invariant
@given(amount=money)
@settings(max_examples=200, deadline=None)
def test_every_builder_produces_a_balanced_transaction(amount: Money) -> None:
    for name, build in ALL_BUILDERS:
        draft = build(ctx(), amount)
        assert draft.imbalance_minor == 0, f"{name} did not balance at {amount}"
```

Example counts are chosen per property, not defaulted: 300 for
`validate` accepting exactly the balanced arrangements and for the reservation
headroom ceiling, 200 for the balancing property and policy totality, 100 for
reversal, 60 for scheduler totality. `assume()` appears once, in the
reservation property, to discard positions where holds exceed funding.

Two shapes recur and are worth copying:

- **Totality.** "Whatever the facts, this never raises and always returns a
  decision." Used for the policy evaluator over the default bundle and for the
  retry scheduler over every failure class.
- **Conservation.** "This arithmetic neither creates nor destroys a minor
  unit." Used for `Money.split`, for every posting builder, and for the
  evidence module's net-of-costs aggregation.

Hypothesis keeps its example database in `.hypothesis/`, which self-excludes
from git. A failing property prints a minimal counterexample and replays it
first on the next run.

---

## Writing a new test

### A pure unit test

Put it next to the others in the file for its module. Name it as a sentence
stating the guarantee — `test_a_gateway_timeout_writes_nothing_off`, not
`test_write_off_2`. Give it a docstring only when the *reason* is not obvious
from the name, and make that docstring say why the guarantee matters rather
than restating the assertion.

```python
def test_a_cap_never_raises_the_proposed_amount() -> None:
    """A ceiling above the request leaves the request alone."""
    decision = evaluate(
        bundle(
            rule("ceiling", 300, PolicyEffect.CAP, cap_amount_minor=5_000_00),
            rule("allow", 900, PolicyEffect.ALLOW),
        ),
        facts(action_type=ActionType.OFFER_WINBACK_DISCOUNT, amount_minor=100_00),
    )
    assert decision.effective_amount == Money(100_00)
```

Use the file's existing builder rather than constructing the object inline, and
extend the builder if you need a new knob. Annotate every parameter and the
return — the test tree is inside ruff's `src` and is linted with the same
rules the package is, minus `S` and `T20`.

### A property

Reach for one when the claim is universal rather than exemplary. If the
sentence you would write in the docstring contains "any", "every" or "never",
it is a property.

```python
@given(minor=st.integers(min_value=0, max_value=10**12), parts=st.integers(1, 40))
def test_allocation_conserves_every_paisa(minor: int, parts: int) -> None:
    """Splitting money never creates or destroys a minor unit."""
    original = Money(minor)
    pieces = original.split(parts)
    assert len(pieces) == parts
    assert sum_money(pieces) == original
```

Bound your strategies to realistic ranges — `st.integers(1, 50_000_000)` for
paise, `st.integers(0, 23)` for an hour — so counterexamples are readable.
Always pass `deadline=None`.

### An invariant test

Add `@pytest.mark.invariant` **and** say in the docstring which of the ten
invariants in `docs/ARCHITECTURE.md` §6 it enforces. Prefer a property over an
example: the marker is a claim about what the module can be made to do, not
about one input. Keep the selection small enough that `make invariants` stays
readable in one screen.

### A graph test

Build only the doubles you are varying and let `make_deps` supply the rest.
Assert against what the doubles recorded, not against internal state.

```python
async def test_a_policy_denial_stops_the_action_executing() -> None:
    gateway = StubGateway("settled")
    deps = make_deps(policy=StubPolicy(PolicyEffect.DENY.value), gateway=gateway)
    final = await run(deps, make_state())
    assert gateway.keys == []
```

No `@pytest.mark.asyncio` — `asyncio_mode = "auto"` covers it. If your test
crosses an interrupt, give it a distinct `thread_id`; `run()` takes one as its
third argument, and reusing a thread across tests will resume the wrong
checkpoint. If the port you need does not exist yet, add the Protocol to
`anvil/graph/ports.py` first, keeping the slice as narrow as the node actually
requires — the port file is read as the statement of how much authority the
orchestrator has.

### An integration test

Nothing to follow yet — the tier is empty. When you add the first one:

1. Put it in `tests/integration/` and mark it `@pytest.mark.integration`. The
   marker is already registered; `--strict-markers` will accept it.
2. It will need `make db-up && make db-wait && make migrate` first, or a native
   Postgres per the README's development section. `make test-all` runs those
   three targets and then the whole suite.
3. Add whatever database fixture it needs to `tests/conftest.py` — that file
   is currently one autouse fixture and has room. Keep the fixture
   session-scoped and roll back rather than truncate, so `pytest tests/unit`
   stays as fast as it is now.

The things worth writing first are the ones the Protocols currently abstract
away and no test therefore reaches: the append-only triggers refusing `UPDATE`
and `DELETE` on `ledger_entries` (invariant 1, migration
`9a1b2c3d4e5f_ledger_immutability.py`), the `SELECT … FOR UPDATE` budget lock
under genuine concurrency (invariant 8 — `test_ledger.py` proves the
arithmetic is right *given* the sequence, and the lock is what produces the
sequence), the webhook dedupe constraint (invariant 4), and resumption from a
Postgres checkpointer after the process dies mid-interrupt (invariant 9).

### An e2e test

Same, in `tests/e2e/` with `@pytest.mark.e2e`. Nothing exists yet, and the API
surface it would drive is still in progress.

---

## What the suite does not cover

Stated plainly, because a test count with an unstated boundary is not evidence.

- **No test touches a database.** Invariants 1 and 4 are database-enforced and
  are asserted today only by the README's transcript of a manual `psql`
  session, not by anything in `tests/`.
- **Invariant 8's row lock is not exercised under concurrency.**
  `test_two_concurrent_concessions_cannot_jointly_overspend` proves the
  arithmetic is correct given that the two requests observe the positions in
  sequence. Producing that sequence is the lock's job, and the lock is untested.
- **Invariant 9's replay is asserted against `MemorySaver`**, not against the
  Postgres checkpointer, so "the process can be killed and the case resumes" is
  demonstrated in-process only.
- **Invariant 10, PII redaction**, has no test file. `anvil/llm/redaction.py`
  is 19 KB of the exact logic that would most reward property tests — Luhn
  arbitration on card candidates, stable pseudonyms, salt derivation — and none
  exist yet.
- **`anvil/api/`, `anvil/audit/`, `anvil/channels/`, `anvil/gateway/`,
  `anvil/mandates/` and `anvil/db/models/` have no tests of their own.** They
  are reached, if at all, through the doubles in `test_graph.py`, which is to
  say not reached.
- **The batch is verified for reproducibility and calibration, not for
  correctness against reality.** `test_simulator.py` pins the issuer's failure
  mix and settle rates to ranges taken from real payment data, which bounds how
  wrong the simulation can be; it does not make it right.
