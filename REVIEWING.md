# For reviewers

A guided path through Anvil for someone with limited time. Written for the
Razorpay AI Buildathon panel, but it works for anyone assessing the codebase.

The repository is large. This document routes you to the parts that carry
signal, in the order that makes them make sense.

---

## Five minutes

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python scripts/tour.py
```

Eight sections of live output. No credentials, no database, no network. Every
number it prints is computed by the same code the tests exercise — nothing in it
is a mock-up.

The three things worth watching for:

1. **`Money.from_major(1499.00)` raises.** Floats are refused by the type, not by
   convention.
2. **`insufficient_funds` waits eleven days; `issuer_technical` retries in six
   hours; `instrument_expired` is refused outright.** That is a dynamic program
   over an issuer hazard curve, with no model involved.
3. **The agent recovers in full with the language model entirely unavailable.**
   The degradation path is real and is exercised on every test run.

---

## Fifteen minutes

```bash
make console      # http://localhost:8000
```

One process, no build step, no database. Four of the seven screens exist so the
claims in this repository can be **falsified rather than believed**:

- **Retry scheduler** — move the failure date and watch the answer change, with
  the 24 rejected hours listed underneath.
- **Policy engine** — throw arbitrary facts at the live bundle and read the
  rule-by-rule trace. Try setting consent to `withdrawn`.
- **Classifier** — `U30` resolves with no model call; `switch busy` escalates.
- **Ledger** — try to make a posting fail to balance.

Then open the **Approval inbox** and approve something. Each item is a genuinely
paused LangGraph thread on a committed checkpoint; approving resumes it, and it
runs authorisation, policy and the executor for real. Approving twice with the
same version returns 409.

---

## Thirty minutes

```bash
make batch
```

The controlled experiment: three arms against the same issuer and the same
customers, assigned by deterministic hash.

**Read the result carefully. The agent currently loses.** Naive fixed-schedule
dunning beats it on raw recovery rate, and the report leads with that rather
than burying it. The calibration table underneath explains why — the retry
curves are hand-written priors, systematically over-confident by about ten
points, not parameters fitted to this issuer.

Tuning the simulator until the agent won would have taken twenty minutes and
would have made every other number in this repository worthless.

---

## What to read, in order

| # | File | Why |
|---|---|---|
| 1 | [`docs/explanation/architecture.md`](docs/explanation/architecture.md) §3 | Where AI is used and, more importantly, the five places it is deliberately **not**, each with the alternative that was considered. |
| 2 | [`anvil/risk/scheduler.py`](anvil/risk/scheduler.py) | The dynamic program. `O(attempts × horizon)` via a suffix maximum. The module docstring derives it. |
| 3 | [`anvil/ledger/posting.py`](anvil/ledger/posting.py) | Pure construction and a total balance check above the line, session I/O below it. The four-legged concession posting is the one to read. |
| 4 | [`anvil/graph/ports.py`](anvil/graph/ports.py) | Twelve Protocols. `LedgerPort` states in one place exactly how much authority the agent has over the books: four economic events and nothing else. |
| 5 | [`anvil/policy/evaluator.py`](anvil/policy/evaluator.py) | Four semantics, each closing a way a policy engine can quietly permit something. **No match denies.** |
| 6 | [`docs/adr/`](docs/adr/) | Twelve architecture decisions with their trade-offs — including the ones that turned out to be wrong. |

---

## Questions worth asking, and where the answer lives

**"What stops the agent overspending?"** Three independent things, and they fail
closed in sequence: the mandate registry (`anvil/mandates/authorise.py`) refuses
an action with no authorisation; the policy engine caps concessions at the
tighter of a rupee ceiling and a percentage of MRR; and the ledger reserves
against a budget row under `SELECT … FOR UPDATE`, so two concurrent cases cannot
jointly overspend. See [ADR-0004](docs/adr/0004-bounded-authority.md).

**"How do I know the ledger is right?"**
`.venv/bin/python -m pytest -m invariant -q`, then try it yourself:

```
psql> UPDATE ledger_entries SET amount_minor = 999999999 WHERE id = '…';
ERROR:  ledger is append-only: UPDATE on ledger_entries is refused.
```

**"What happens when the model hallucinates?"** It is refused before execution
and counted as a model-safety event, which the batch report surfaces as a
first-class metric. `tests/unit/test_graph.py::test_an_out_of_bounds_proposal_is_refused_and_counted`.

**"What happens when Razorpay times out?"** Nothing is posted. The case parks in
`PENDING_RECONCILIATION`, which is deliberately non-terminal, and a reconciler
polls with the original idempotency key. It is never blind-retried, because a
timeout means the outcome is unknown, not failed. See
[ADR-0009](docs/adr/0009-unknown-gateway-outcomes.md).

**"Is any of this actually finished?"** Some of it is not, and
[the README says exactly which parts](README.md#honest-limitations). `audit` is
empty; `mandates`, `llm`, `gateway` and `channels` are partial; the batch runs on
the deterministic fallback rather than a live Claude client.

---

## What I would ask about, in your position

The retry curves are the weakest part of the system and I would open there. They
are priors I wrote by hand, the calibration report says they are miscalibrated,
and the fix — fitting them to observed outcomes — is specified in
`anvil/risk/calibration.py` but has not been run. Ask what I would fit them on,
and what happens to a merchant with too little history to fit anything.
