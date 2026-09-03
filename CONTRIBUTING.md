# Contributing to Anvil

There is no CI. `.github/` does not exist, and no workflow runs on push. The
loop below is the only gate this repository has, so run it before every commit
rather than trusting something downstream to catch you.

---

## Setup

Offline mode is the default and needs no Razorpay or Anthropic credentials.

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"
```

Or `make venv`, which does exactly that. Python 3.12 or newer is required
(`requires-python = ">=3.12"`).

Verify the install by running the guided tour — it exercises the real code
across eight sections and needs nothing but the venv:

```bash
.venv/bin/python scripts/tour.py     # or: make tour
```

`make console` serves the web console at `http://localhost:8000` via
`uvicorn anvil.main_api:app --reload`, with no database and no keys. The API
module behind it is work in progress.

### The database

Nothing in `tests/unit/` needs one, so you can develop most of the codebase
without it. When you do need it, use a native Postgres:

```bash
brew install postgresql@16 && brew services start postgresql@16
createdb -h 127.0.0.1 anvil
.venv/bin/alembic upgrade head        # 33 tables + append-only triggers
```

`docker-compose.yml` is committed but **unverified** — local development moved
to native Postgres after Docker Desktop failed on the development machine. The
Makefile's `db-up`, `db-wait`, `demo` and `down` targets all go through
`docker compose` and inherit that caveat. `make seed` invokes
`python -m anvil.simulator.seed`, which does not exist yet.

### Configuration

Copy `.env.example` to `.env` if you need to change anything. Every setting has
a working default, so you usually do not. Settings are read through
`anvil/core/config.py`, prefixed `ANVIL_`, and the `Settings` object is frozen
and `lru_cache`d — read it via `get_settings()`, never construct one.

`ANVIL_SEED` defaults to `20260902`. That number appears in `.env.example`, in
`Settings.seed`, in two test files and in `make batch`. Do not change it
casually: it is what makes a batch reproducible across machines.

---

## The loop

```bash
make fmt      # ruff format anvil tests && ruff check --fix anvil tests
make lint     # ruff check && ruff format --check && mypy anvil
make test     # pytest tests/unit -q          -- 203 tests, no database
```

Written out, in the order to run them:

```bash
.venv/bin/ruff format anvil tests
.venv/bin/ruff check --fix anvil tests
.venv/bin/ruff check anvil tests
.venv/bin/ruff format --check anvil tests
.venv/bin/mypy anvil
.venv/bin/python -m pytest tests/unit -q
.venv/bin/python -m pytest -m invariant -q     # or: make invariants
```

### What the tools are configured to do

All configuration lives in `pyproject.toml`. There is no `.ruff.toml`, no
`mypy.ini`, no `pytest.ini`, no `setup.cfg`.

**ruff** — line length 100, target `py312`, `src = ["anvil", "tests", "scripts"]`.
The selected rule families are `E F I N UP B A C4 SIM RUF ASYNC S DTZ T20`. Two of
those carry real design weight: `DTZ` is what stops a naive `datetime` reaching
the money path, and `T20` is what stops a stray `print` reaching a log
aggregator. Global ignores are `S101` (assert), `B008` (function call in a
default argument, which FastAPI's `Depends` requires), `N818` (error class
naming) and `RUF012` (mutable class attribute).

Per-file ignores exist and each is deliberate — read them before adding a
`noqa`:

| Path | Ignored | Why |
|---|---|---|
| `tests/**` | `S`, `T20` | asserts and prints are the point |
| `scripts/**` | `T20` | the tour's job is to print |
| `anvil/simulator/**` | `S311` | seeded pseudo-randomness is the point, and it is never used for anything that needs to be unguessable |
| `anvil/evidence/statistics.py` | `S311` | same, for the bootstrap |
| `anvil/evidence/run_batch.py` | `T201` | a command-line entry point whose job is to print a report |
| `scripts/tour.py` | `T201`, `E402`, `ASYNC240` | it prints, and it inserts the repo root on `sys.path` before importing |

Prefer a per-file ignore in `pyproject.toml` with a comment over a scattered
`# noqa`. `RUF100` is on, so a `noqa` for a rule that is not enabled is itself
an error.

Note that `make lint` and `make fmt` pass `anvil tests` only. `scripts/` is
inside ruff's `src` and has its own ignores, but the Make targets do not reach
it — lint it explicitly if you change the tour.

**mypy** — `strict`, `python_version = "3.12"`, the `pydantic.mypy` plugin,
`warn_unreachable`, `disallow_any_generics`, `ignore_missing_imports`. It runs
over `anvil` only; `tests` is linted by ruff but not type-checked, though the
test files are fully annotated anyway and new tests should be too.

### The honest state of the loop

`ruff check` and `ruff format --check` both pass clean across `anvil` and
`tests`, and the commits that say "lint clean" mean exactly that.

`mypy anvil` under `strict` **does not currently pass** — the last run reported
21 errors across 12 files, spread over `policy/`, `graph/nodes/`, `simulator/`,
`domain/money.py` and the in-progress `api/`. Treat mypy as the standard the
code is written to and the direction of travel: do not add new errors, and
clear the ones in any file you touch. When the count reaches zero, delete this
paragraph.

---

## Conventions

These are not aspirations. Every one of them is observable in the committed
code, and the fastest way to write code that fits here is to read the module
nearest to what you are building.

### Module docstrings state the reason, not the contents

This is the strongest convention in the repository, and the one most worth
matching. Every module opens with a docstring that explains *why the module is
shaped the way it is*, usually by naming the alternative that was rejected and
what it would have cost. `anvil/graph/ports.py`, `anvil/simulator/rng.py`,
`anvil/evidence/assignment.py`, `anvil/evidence/statistics.py` and
`anvil/llm/redaction.py` are the models to copy. A docstring that restates the
module name adds nothing; the code already says what it does.

The same applies to test docstrings. `"""A reversal restores every account it
touched to its prior position."""` is worth having. `"""Test reversal."""` is
not.

### Typing

- `from __future__ import annotations` at the top of every module, without
  exception.
- Modern built-in generics and unions: `dict[str, Any]`, `str | None`,
  `tuple[str, ...]`. Never `Dict`, `Optional` or `Union`.
- Keyword-only parameters. Anything with more than two arguments takes `*,`
  first. Every port method, every posting builder, every scheduler entry point.
- Value objects are `@dataclass(frozen=True, slots=True)`. `Money`, `Deps`,
  `Interval`, `BudgetPosition`, `EntryDraft` all follow it.
- Seams are `Protocol`s, `@runtime_checkable` where something checks them. The
  twelve in `anvil/graph/ports.py` and `Clock` in `anvil/core/clock.py`.
- Closed vocabularies are `StrEnum` in `anvil/domain/enums.py`. A new state,
  action or failure class goes there, not into a string literal.
- Module constants are `SCREAMING_SNAKE`, typed `Final` where they are simple,
  and documented with a `#:` comment above them so the reason survives.

### Naming

- `_minor` — an integer count of currency minor units. `amount_minor`,
  `cap_amount_minor`, `budget_headroom_minor`.
- `_bps` — basis points. Every probability, rate and score ratio in the
  codebase is an integer in basis points, never a float.
- `_at` — a timezone-aware `datetime`. `effective_at`, `failed_at`,
  `original_failure_at`.
- British spelling in domain vocabulary, consistently: `authorise`,
  `recognise`, `summarise`, `realised`, `utilisation`, `behaviour`. Match it.

### Module layout

The package is organised by domain concern, not by technical layer — there is
no `models/`, `services/`, `utils/` split. `anvil/domain/` is the contract
everything else builds against and imports nothing from the rest of the
package. `anvil/core/` is plumbing: config, clock, ids, errors, logging.
`anvil/graph/` is the orchestrator, and it imports **none** of the modules it
drives.

If a new module would need to import six others, that is the signal to declare
a Protocol in `anvil/graph/ports.py` instead and let the composition root
supply the implementation. Keep the port's slice as narrow as the caller
actually requires — the port file is read as the statement of how much
authority the orchestrator has, and widening it is a design decision, not a
convenience.

### Error handling

Every failure in Anvil is one of the classes in `anvil/core/errors.py`. Do not
define an ad-hoc exception in a module; add it to the taxonomy. Each carries a
stable `code` for the API surface, an `http_status`, a `retryable` flag and
free-form `**context`, so retry behaviour is a property of the error *type*
rather than a judgement call scattered across call sites.

Four families, and which one you inherit from is the decision:

| Family | Meaning | Handling |
|---|---|---|
| `InvariantViolation` | the system is wrong | never caught; abort the transaction and page a human |
| `DomainError` | the system is right and is saying no | expected; surfaced to the caller with its reason |
| `ConflictError` | two writers raced | resolved, often as a `200` (`DuplicateEvent`, `StaleEvent`) |
| `ExternalError` | a boundary failed | backoff, except where the outcome is unknown |

Two carry design weight worth knowing before you catch anything.
`GatewayTimeout` is `retryable = False` on purpose: the outcome is genuinely
unknown, and the only correct response is reconciliation with the same
idempotency key, never a blind retry. `ModelProposedOutOfBounds` is recorded as
a first-class model-safety event and surfaced as a metric — hiding these would
defeat the point of measuring them.

Never catch `InvariantViolation`. A system that silently recovers from an
unbalanced ledger is a system that silently loses money.

### Money

- Integer minor units, always. `Money` is `(int paise, Currency)`. Floats are
  refused by the type: `Money.from_major(1499.00)` and `Money(149900).scale(0.15)`
  both raise `TypeError`. Use `Decimal` for the major-unit form.
- Direction is carried by `EntryDirection`, not by sign. Ledger entries must be
  strictly positive.
- Division only in forms that provably conserve the total. `Money.split(n)`
  returns `n` pieces summing exactly to the original; `Money.percent(bps)` is
  exact.
- Cross-currency arithmetic raises rather than coercing.

### No money without a posting

The discipline the whole architecture rests on, and the one to be most careful
with in review.

- **The graph cannot post arbitrarily.** `LedgerPort` has no `post` method and
  no way to construct an entry. It exposes four economic events —
  `recognise_receivable`, `settle_recovered`, `grant_concession`, `write_off` —
  plus reserve, release and settle for a concession hold. If you find yourself
  wanting a fifth, that is a chart-of-accounts change with a migration, not a
  new method on the port.
- **Every transaction balances before anything is written.** `validate()`
  refuses an imbalance, a single-entry transaction, and a transaction spanning
  two merchants.
- **Recognise before you recover.** The receivable is posted at case open so a
  later write-off reduces a real asset. The ordering is asserted by a test.
- **Corrections are reversals, never edits.** `reverse_draft` builds a mirrored
  draft referencing the original. Postgres refuses `UPDATE` and `DELETE` on
  `ledger_entries` outright (migration `9a1b2c3d4e5f_ledger_immutability.py`),
  so an edit is not merely discouraged, it is impossible.
- **Balances are derived, never stored.** There is no balance column and no
  `UPDATE` against one anywhere.
- **An unknown outcome posts nothing.** A gateway timeout writes no settlement
  and no write-off; the case parks in `PENDING_RECONCILIATION`. Recording a
  recovery you cannot confirm is worse than recording nothing, and writing off
  money whose fate is unknown is a claim you cannot support.
- **A concession reserves before it spends.** Take the hold, then settle or
  release it. Headroom subtracts both settled and held amounts, because a hold
  that might be released is not headroom you can promise elsewhere.
- **Idempotency keys are a pure function of intent.** No timestamp, no fresh
  id, nothing that varies per call. That is what makes a network retry collapse
  instead of paying twice.

### Time and randomness

Nothing calls `datetime.now()` directly. Take a `Clock` and call `clock.now()`;
`FrozenClock` is what lets a test place the system at any instant and lets the
batch run thirty days in a second. Naive datetimes are refused at runtime by
`_require_aware` and statically by ruff's `DTZ` rules — every datetime is
timezone-aware, computed in `IST` where the concept is an IST one (quiet hours,
salary cycles, issuer maintenance windows) and stored in UTC.

Nothing uses an unseeded generator. In the simulator, take a substream:
`substream(seed, "attempt", attempt_id)`. In the evidence module, take an
explicit `seed=` argument. Avoid `log`, `exp` and `gauss` on any path that must
reproduce — they route through libm and are not bit-identical across platforms;
build skewed distributions from integer draws and exact `Decimal` ratios
instead.

### Logging

`structlog`, via `anvil/core/logging.py`, which redacts on write. A stray
`log.info("charging", vpa=customer.vpa)` cannot leak an identifier because
`SENSITIVE_KEYS` is masked wherever it appears in an event. Do not add a
redaction step at the read side; do add a key to `SENSITIVE_KEYS` if you
introduce a new identifier. `print` is a ruff error outside `scripts/` and
`run_batch.py`.

---

## Tests

Every change needs a test, and the test should state a guarantee rather than
exercise a line. `docs/TESTING.md` covers the suite in full: the tiers, the
markers, the doubles, how determinism is achieved, how the model is faked, and
how to write each kind of test.

The short version:

- Put it in `tests/unit/`, in the file for its module. The integration and e2e
  tiers exist as empty packages; nothing is in them yet.
- Name it as a sentence — `test_a_gateway_timeout_writes_nothing_off`.
- `async def test_…` needs no decorator; `asyncio_mode = "auto"` is set.
- Use the file's existing builder (`make_deps`, `ctx`, `facts`, `rule`) rather
  than constructing objects inline, and extend the builder if you need a knob.
- Reach for Hypothesis when the claim contains "every", "any" or "never".
  Always pass `deadline=None`.
- Add `@pytest.mark.invariant` only when failing the test means money moved
  wrongly, and say in the docstring which of the ten invariants in
  `docs/ARCHITECTURE.md` §6 it enforces. `--strict-markers` is on, so an
  unregistered marker is an error; register new ones in `pyproject.toml`.

---

## Commit messages

The style is visible in `git log`. Nine commits, all consistent.

**Subject.** A declarative phrase naming what landed. Capitalised, no trailing
period, no `feat:`/`fix:` prefix. Usually `Area: the pieces`, sometimes a plain
sentence:

```
Ledger: posting, balances, reservations, and structural immutability
Risk: DP retry scheduler, scoring, calibration, detection
Graph: the recovery state machine, with two durable interrupts
Lint clean across every module: sorted exports and imports, PEP 695 type parameters
README, a local build board, and an honest correction about UAP
```

**Body.** Wrapped at about 76 columns. A sentence or two of prose stating what
the commit establishes and why, then a `- path: what and why` list. The bodies
in this repository explain reasoning, not diffs — they name the alternative
that was rejected, the defect that was found while building, and the result
that came out worse than hoped. `238bcd9` leads its own summary with the
finding that the naive baseline beat the agent. Match that: a commit message
that only flatters the change is not the house style.

**Status line.** End the body with the test count and lint state:
`203 tests, lint clean.`

**Trailer.** `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`

A trimmed real example:

```
Batch experiment: three arms, seeded, with an honest report

The track's stated bar is measured money recovered across batches. It now
runs: make batch.

- simulator/issuer.py: ground truth, as an additive hazard model rather than a
  product of multipliers, so each term is a probability of one specific thing
  going wrong and can be argued with individually.
- evidence/: bootstrap intervals on the *difference* rather than two intervals
  eyeballed for overlap, a z-test cross-check, minimum detectable effect, and a
  report that says "not significant" in those words.

Four defects found by running it, each fixed:
- The simulated clock never advanced, so every retry was presented at the
  instant of failure and the scheduler's choice of hour changed nothing.
- merchant_review_first was not declared in RecoveryState, so LangGraph
  silently dropped it and every merchant looked like review-first.

203 tests, lint clean.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

---

## Before opening a PR

Branch off `main`; do not commit to it directly.

```bash
make fmt
make lint
make test
make invariants
```

Then, in order:

1. **`make fmt` and `make lint` are clean** for every file you touched. Ruff
   must pass outright. Mypy must not report anything new in your files, and
   should report less than before if you touched one of the twelve that are not
   yet clean.
2. **`make test` passes** — 203 tests today, and your number should be higher.
   State the new count in the commit body.
3. **`make invariants` passes.** It is a subset of the above, but run it
   separately so a failure there is unmissable.
4. **`make batch` still runs and still reproduces.** If you touched
   `simulator/`, `evidence/`, `risk/` or `graph/`, run it twice with the same
   seed and diff the reports. A change that quietly breaks reproducibility
   invalidates every number in the submission.
5. **`.venv/bin/python scripts/tour.py` still completes** if you touched
   anything it demonstrates. It runs real code across eight sections and is the
   fastest smoke test in the repository.
6. **Documentation is updated in the same commit.** A new invariant goes in
   `docs/ARCHITECTURE.md` §6 *and* gets an `invariant`-marked test. A new
   module goes in the README's layout tree. A changed test tier goes in
   `docs/TESTING.md`. Counts stated in prose — test counts, table counts, rule
   counts — must be verified, not carried forward. The README currently says
   145 tests, which was true two commits ago and is not now; do not add a
   second one of those.
7. **Nothing overclaims.** This is a submission judged partly on honesty, and
   `ef70143` exists specifically to walk back an overclaim about NPCI's
   Unified Agent Protocol. If a result is worse than hoped, lead with it. If a
   module is partial, say which half is done. If a number is not measured, do
   not print it.

---

## Where the documentation lives

| File | Contents |
|---|---|
| `README.md` | the problem, the thesis, where AI is and is not used, the scheduler's real output, the ten invariants, failure modes, honest limitations |
| `docs/ARCHITECTURE.md` | the full design in fourteen sections. §6 is the ten financial invariants — the spine of the submission. §7 is the decline taxonomy and retry science, §10 the recovery graph, §11 the evidence methodology |
| `docs/TESTING.md` | the test suite: tiers, markers, fixtures, determinism, the doubles, how to write each kind of test, and what is not covered |
| `docs/DESIGN.md` | the console design system, binding on everything under `console/` |
| `docs/board.html` | a standalone visual build board; open it in a browser |
| `CONTRIBUTING.md` | this file |
| `.env.example` | every setting, with the offline defaults that need no credentials |
| `scripts/tour.py` | the guided tour — executable documentation of what the system does |

Module-level docstrings are load-bearing documentation here rather than
boilerplate, and for anything below the architecture level they are the primary
source. `anvil/graph/ports.py` is the authoritative statement of how much
authority the orchestrator has; `anvil/core/errors.py` is the authoritative
error taxonomy; `anvil/simulator/rng.py` is the authoritative account of the
reproducibility guarantee. Keep them current with the code in the same commit.
