# Contributing

## Getting set up

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/unit -q     # 203 tests, no database needed
make console                                 # http://localhost:8000
```

Postgres is only needed for the integration tests and migrations:

```bash
brew install postgresql@16 && brew services start postgresql@16
createdb -h 127.0.0.1 anvil && .venv/bin/alembic upgrade head
```

## Before you open a pull request

```bash
make lint    # ruff check, ruff format --check, mypy
make test    # the unit suite
```

Both must pass. `make fmt` fixes most lint findings automatically.

## The rules that are not negotiable

These exist because this is a payments system, and each of them has a failure
mode that is silent.

**No floats in the money path.** `Money` is an integer count of minor units and
refuses to be constructed from a float. If you find yourself wanting a float,
what you want is `Decimal` or a different unit.

**No `datetime.now()`.** Take a `Clock`. Time-dependent logic that cannot be
placed at an arbitrary instant cannot be tested, and half of this system's
decisions are about *when*.

**No naive datetimes.** `UTCDateTime` rejects them at the database boundary.

**Never mutate the ledger.** There is no code path that updates or deletes a
posted entry, and Postgres triggers refuse it anyway. Corrections are mirrored
reversal transactions that reference the original.

**Every money-moving call carries a caller-generated idempotency key**, derived
from the *intent* and never from the attempt. A key that varies per call turns a
network retry into a double charge.

**The model proposes; it never decides.** If you are adding an LLM call, the
first question to answer in the docstring is why a deterministic implementation
would be worse. `docs/explanation/architecture.md` §3 is the standard to meet.

## Style

**Docstrings explain why, not what.** The signature already says what. A reader
three months from now needs to know which alternative you rejected and on what
grounds. Read `anvil/ledger/posting.py` for the house voice — precise, calm, and
willing to admit a trade-off.

Full type annotations. `from __future__ import annotations`. Line length 100.
No emoji anywhere. Ruff's configuration in `pyproject.toml` is the arbiter.

## Tests

Unit tests must pass with **no database and no network**. Use fakes, a
`FrozenClock`, and in-memory doubles. Anything that genuinely needs Postgres goes
in `tests/integration/` behind `@pytest.mark.integration`.

Reach for `hypothesis` when the property *is* the specification. "This particular
posting balances" is a much weaker claim than "no posting this module can
construct fails to balance", and only the second is worth the ledger's
reputation.

Tests enforcing a numbered invariant from `docs/explanation/architecture.md` §6
are marked `@pytest.mark.invariant` and can be run alone with `make invariants`.

## Architecture decisions

Anything that changes a module boundary, an invariant, or a dependency direction
wants an ADR in `docs/adr/`. Copy `docs/adr/000-template.md`, take the next
number, and open it as part of the pull request that implements it. An ADR
written afterwards is a summary; one written alongside is a decision.

Superseding an existing decision is normal and expected. Mark the old one
`Superseded by ADR-0NNN` rather than editing it — the record of having changed
your mind is the useful part.
