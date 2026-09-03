# Anvil — Operations

Setup, configuration and day-to-day operation. `README.md` explains what Anvil does and why;
`docs/ARCHITECTURE.md` explains how it is built. This document explains how to run it.

Everything below describes what the code in this repository actually does today. Where a target,
service or module is declared but not yet implemented, that is stated rather than glossed over.

---

## 1. Prerequisites

| Requirement | Why | Notes |
|---|---|---|
| Python **3.12 or newer** | `pyproject.toml` sets `requires-python = ">=3.12"`; ruff targets `py312` and mypy is pinned to `python_version = "3.12"` | The checked-in `.venv` on this machine is 3.14.7. The Docker image is `python:3.12-slim`. |
| **PostgreSQL 16** | Migrations, the append-only triggers, and the LangGraph Postgres checkpointer | Optional for the API, the tour and the batch — none of them open a database connection. |
| A C toolchain / `libpq` | `asyncpg` and `psycopg[binary]` wheels | Usually already present on macOS with Homebrew Postgres installed. |
| Docker (optional) | Only for the `db-up` / `demo` / `test-all` Make targets | Not required for any path in this document except §4.2. |

No Razorpay account, Anthropic API key, or network access is required. Offline mode is the default
and is the only mode with a complete code path today.

---

## 2. First-time setup

```bash
git clone <repo> && cd Razorpay-Proj
make venv                                  # python3 -m venv .venv && pip install -e ".[dev]"
```

`make venv` is the only bootstrap step. Every other Make target invokes `.venv/bin/python`,
`.venv/bin/pip`, `.venv/bin/alembic` or `.venv/bin/ruff` by absolute path, so the virtualenv must
live at `.venv/` in the repository root. Nothing activates it for you and nothing needs you to.

Verify the install without touching a database:

```bash
make tour                                  # eight sections of live output, no DB, no network
make test                                  # 203 unit tests
```

Configuration is optional. `.env.example` documents every variable but is **not** read — pydantic
reads `.env`. Copy it only if you want to change something:

```bash
cp .env.example .env
```

---

## 3. Configuration

### 3.1 How settings are loaded

`anvil/core/config.py` defines a single frozen `Settings` model:

- **Prefix** — every variable is `ANVIL_` + the field name, upper-cased.
- **Sources** — process environment first, then a `.env` file **in the current working directory**.
  Run commands from the repository root or the file is not found.
- **Unknown keys are ignored** (`extra="ignore"`), so a typo in `.env` is silent. Check
  `/health` or a log line rather than assuming a value took effect.
- **Cached and frozen** — `get_settings()` is `@lru_cache(maxsize=1)` and the model is immutable.
  A configuration change requires a process restart; it cannot be applied to a running server.

### 3.2 Environment variable reference

Every value has a working default. The **Required** column means required *for the process to
start*, not merely recommended.

#### Runtime

| Variable | Purpose | Default | Required |
|---|---|---|---|
| `ANVIL_MODE` | `offline` or `live`. Offline drives the in-process simulator and needs no credentials. `live` triggers the credential check in §3.3 and switches the redaction salt from deterministic to random. | `offline` | No |
| `ANVIL_ENV` | Deployment label: `local`, `ci` or `demo`. Validated, carried on `Settings`, and not currently branched on by any module. | `local` | No |
| `ANVIL_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. Sets the structlog filtering bound logger and the stdlib root level. | `INFO` | No |
| `ANVIL_LOG_FORMAT` | `console` (human, coloured when stderr is a TTY) or `json` (one object per line). | `console` | No |
| `ANVIL_SEED` | Positive integer. Every stochastic component derives from it: the API's world, the redaction pseudonymisation salt in offline mode. The same seed reproduces the same run. Rejected if `<= 0`. | `20260902` | No |

#### Infrastructure

| Variable | Purpose | Default | Required |
|---|---|---|---|
| `ANVIL_DATABASE_URL` | SQLAlchemy async URL. Read by `alembic/env.py` (converted to `postgresql+psycopg://` via `Settings.sync_database_url`) and by `anvil/db/session.py`. `Settings.raw_database_url` strips the driver for libraries that build their own connection. | `postgresql+asyncpg://anvil:anvil@localhost:5432/anvil` | No — but migrations need it to point at a real database |
| `ANVIL_REDIS_URL` | Declared on `Settings` and passed to the containers by `docker-compose.yml`. **No module in `anvil/` reads it yet.** | `redis://localhost:6379/0` | No |
| `ANVIL_DB_POOL_SIZE` | `create_async_engine(pool_size=...)`. | `20` | No |
| `ANVIL_DB_MAX_OVERFLOW` | `create_async_engine(max_overflow=...)`. | `10` | No |
| `ANVIL_DB_STATEMENT_TIMEOUT_MS` | Sent as the Postgres `statement_timeout` server setting on every connection, alongside `application_name=anvil`. | `15000` | No |

#### Live-mode credentials

| Variable | Purpose | Default | Required |
|---|---|---|---|
| `ANVIL_RAZORPAY_KEY_ID` | Razorpay API key id. | `""` | Only when `ANVIL_MODE=live` |
| `ANVIL_RAZORPAY_KEY_SECRET` | Razorpay API secret. Held as a `SecretStr`, so it does not appear in a `repr`. | `""` | Only when `ANVIL_MODE=live` |
| `ANVIL_RAZORPAY_WEBHOOK_SECRET` | HMAC-SHA256 secret for `X-Razorpay-Signature`. An empty secret makes `verify_signature` return `False` for every delivery — a misconfigured deployment accepts nothing, not everything. | `""` | Only when `ANVIL_MODE=live` |
| `ANVIL_ANTHROPIC_API_KEY` | Anthropic API key. | `""` | Only when `ANVIL_MODE=live` |

#### Models

| Variable | Purpose | Default | Required |
|---|---|---|---|
| `ANVIL_MODEL_PLANNER` | Model id for recovery planning. Reported by `/health` in live mode. | `claude-opus-5` | No |
| `ANVIL_MODEL_CLASSIFIER` | Model id for failure diagnosis, used only where the deterministic taxonomy gives up. | `claude-sonnet-5` | No |
| `ANVIL_MODEL_COMPOSER` | Model id for customer message composition. | `claude-sonnet-5` | No |

#### Guardrails

| Variable | Purpose | Default | Required |
|---|---|---|---|
| `ANVIL_WEBHOOK_TOLERANCE_SECONDS` | Symmetric replay window for inbound webhooks. Skew in either direction beyond this is a 400. Matches `webhooks.DEFAULT_TOLERANCE_SECONDS`. | `300` | No |
| `ANVIL_LLM_MAX_RETRIES` | Retry budget for model calls. | `3` | No |
| `ANVIL_LLM_TIMEOUT_SECONDS` | Per-call model timeout. | `60` | No |
| `ANVIL_LLM_MAX_OUTPUT_TOKENS` | Output ceiling for model calls. Not listed in `.env.example`. | `4096` | No |
| `ANVIL_FIXTURES_DIR` | Where offline mode looks for recorded model responses. Not listed in `.env.example`. | `anvil/llm/fixtures` | No |

### 3.3 Live mode fails fast

`Settings` carries a model validator: if `ANVIL_MODE=live` and any of the four credentials is
blank, construction raises before anything starts, naming exactly which are missing.

```
ANVIL_MODE=live requires: ANVIL_RAZORPAY_KEY_ID, ANVIL_RAZORPAY_KEY_SECRET,
ANVIL_RAZORPAY_WEBHOOK_SECRET, ANVIL_ANTHROPIC_API_KEY.
Unset ANVIL_MODE to run fully offline with no credentials.
```

Live mode currently satisfies the configuration contract only. The live Razorpay HTTP client and
the Anthropic client are not implemented — `anvil/gateway/` contains event parsing, contracts and
webhook verification, and `anvil/llm/` contains redaction. Offline is the mode that runs end to end.

---

## 4. Running it

### 4.1 Native Postgres (the supported path)

Local development on this machine uses a native Homebrew Postgres 16, not Docker.

```bash
brew install postgresql@16
brew services start postgresql@16

createuser -h 127.0.0.1 -s anvil          # matches the default ANVIL_DATABASE_URL
createdb  -h 127.0.0.1 -O anvil anvil

.venv/bin/alembic upgrade head            # 33 tables + the append-only triggers
```

If you would rather use your own role, set `ANVIL_DATABASE_URL` instead of creating an `anvil` user:

```bash
export ANVIL_DATABASE_URL="postgresql+asyncpg://$(whoami)@127.0.0.1:5432/anvil"
```

Then run the API:

```bash
.venv/bin/uvicorn anvil.main_api:app --port 8000 --reload
```

The API does **not** open a database connection. `/health` says so explicitly
(`"database": "not required — the console runs from the seeded simulator"`). Postgres is needed for
migrations, for the ledger immutability demonstration, and for the integration and e2e test
markers — not for the console.

### 4.2 Docker Compose

`docker-compose.yml` defines five services. Their current state:

| Service | Image / build | Ports | State |
|---|---|---|---|
| `postgres` | `postgres:16-alpine`, `--data-checksums`, `max_connections=200`, `shared_buffers=256MB`, `log_min_duration_statement=500` | `5432:5432` | Works. Volume `anvil-pgdata`. Healthcheck `pg_isready -U anvil -d anvil`. |
| `redis` | `redis:7-alpine` | `6379:6379` | Starts, but no application code reads `ANVIL_REDIS_URL`. |
| `api` | Built from `Dockerfile` | `8000:8000` | Works. Waits for `postgres` and `redis` to be healthy. |
| `worker` | Built from `Dockerfile`, `python -m anvil.main_worker` | — | **Fails: `anvil/main_worker.py` does not exist.** |
| `console` | `build: ./console` | `3000:3000` | **Fails: the `console/` directory was removed.** The console is now a single static file served by the API at `/`. |

So `docker compose up` in full will not come up. What works:

```bash
make db-up                                 # docker compose up -d postgres redis
make db-wait                               # block until pg_isready succeeds inside the container
make migrate
```

The compose Postgres publishes on host port 5432, which collides with a running Homebrew Postgres.
Pick one; see §9.

The `Dockerfile` is a two-stage build: the builder compiles wheels with `build-essential` and
`libpq-dev`, the runtime installs them into `python:3.12-slim` with only `libpq5` and `curl`, runs
as the non-root user `anvil` (uid 10001), exposes 8000, and health-checks
`curl -fsS http://localhost:8000/health` every 10s after a 20s start period. It copies `anvil/`,
`alembic/` and `alembic.ini` — **not** `scripts/`, so the guided tour is not runnable inside the
image.

### 4.3 The console

The console is `anvil/api/static/index.html`, a single self-contained page. There is no Node
toolchain, no build step and no CORS: `anvil/api/app.py` mounts `/static` and serves the file at
`/` when the directory exists. It calls the same-origin API — `/api/approvals`, `/api/cases/{id}`,
`/api/batch`, `/api/policy/bundle`, `/api/policy/evaluate`, `/api/taxonomy`, `/api/classify` — and
stores only a theme preference in `localStorage`. It links Google Fonts; without network access it
falls back to the system stack and everything else still renders.

| URL | What it is |
|---|---|
| `http://localhost:8000/` | The console |
| `http://localhost:8000/docs` | OpenAPI / Swagger UI (ReDoc is disabled) |
| `http://localhost:8000/health` | Liveness plus mode, version, model and seed |

### 4.4 What startup does

The FastAPI lifespan (`anvil/api/app.py`) configures logging, reads settings, then calls
`anvil.api.state.initialise()`, which:

1. Builds a population of **900 subscriptions** from `ANVIL_SEED` at a fixed epoch
   (`2026-09-01T06:00Z`), so a session in December looks identical to one in September.
2. Runs cases with `merchant_review_first` left on until **8** of them pause on a real LangGraph
   interrupt, filling the approval queue with genuinely suspended threads.
3. Logs `anvil_api_ready` with `mode`, `seed`, `at_risk_cases` and `pending_approvals`.

Approving from the console resumes that thread, which then runs authorisation, policy and the
executor for real. The queue holds an optimistic `version` per item: a second operator resolving a
stale version gets a `409 optimistic_lock_conflict` rather than overwriting the first decision.

### 4.5 API surface

| Method | Path | Notes |
|---|---|---|
| `GET` | `/health` | `status`, `mode`, `version`, `database`, `model`, `seed` |
| `GET` | `/api/taxonomy` | 76 codes across four namespaces (`upi` 20, `nach` 13, `card` 18, `text` 25), the 10 failure classes and their retry postures |
| `POST` | `/api/classify` | `raw_code`, `gateway_description`, `bank_narration`, `rail_hint` → resolved class or `would_call_model: true` |
| `GET` | `/api/scheduler/explain` | `failure_class` (required), `amount_minor`, `failed_at`, `attempts_used`, `mandate_attempts_remaining` |
| `GET` | `/api/policy/bundle` | The live bundle, its version and content hash |
| `POST` | `/api/policy/evaluate` | Arbitrary `PolicyFacts`; unknown keys are a 422 at the boundary |
| `GET` | `/api/ledger/demo` | `at_risk_minor`, `concession_minor`, `recover` → validated, balanced posting drafts (nothing is written) |
| `GET` | `/api/cases` | `limit` 1–500 (default 60), `unmapped_only` |
| `GET` | `/api/cases/{case_id}` | Timeline and per-action legitimacy trail |
| `GET` | `/api/approvals` | Every paused graph awaiting a person |
| `POST` | `/api/approvals/{approval_id}` | `{decision: approve\|reject\|edit, decided_by, version, note?, edited_amount_minor?}` |
| `GET` | `/api/batch` | `seed`, `size` 100–4000 (default 2000), `with_model`; cached per `(seed, size, with_model)` |

There is **no webhook endpoint mounted.** `anvil/gateway/webhooks.py` is a library today; §8
documents the contract it implements.

---

## 5. Make targets

`make` with no argument prints the same list. Every target assumes `.venv/` exists.

| Target | Command | What it does |
|---|---|---|
| `help` | — | Default goal. Greps the `## ` comments out of the Makefile. |
| `venv` | `python3 -m venv .venv && pip install -e ".[dev]"` | Bootstrap. Run once. |
| `db-up` | `docker compose up -d postgres redis` | Data services only, no application containers. |
| `db-wait` | `docker compose exec -T postgres pg_isready` | Blocks until the containerised Postgres accepts connections, then prints `postgres ready`. Loops forever if the container is not running. |
| `migrate` | `.venv/bin/alembic upgrade head` | Applies both revisions. Uses `ANVIL_DATABASE_URL`, so it works against native or containerised Postgres. |
| `seed` | `python -m anvil.simulator.seed` | **Broken: the module does not exist.** There is no database seeding step; see §6.3. |
| `demo` | `db-up db-wait migrate seed` + `docker compose up -d` | **Fails at `seed`**, and the `console` and `worker` services cannot build. Use §4.1 instead. |
| `batch` | `python -m anvil.evidence.run_batch --seed $(SEED) --size $(SIZE)` | The seeded experiment. `SEED ?= 20260902`, `SIZE ?= 3000`; override on the command line. |
| `batch-with-model` | same, plus `--with-model` | Models the LLM classifier as available so its contribution is measured rather than asserted. |
| `tour` | `python scripts/tour.py` | Eight sections of live output. No database, no credentials, no network. |
| `test` | `pytest tests/unit -q` | 203 tests, no database. |
| `test-all` | `db-up db-wait migrate` then `pytest -q` | The integration and e2e packages are currently empty, so this runs the same tests behind a Docker dependency. |
| `invariants` | `pytest -q -m invariant` | The 8 financial-invariant tests in `tests/unit/test_ledger.py`. |
| `lint` | `ruff check` + `ruff format --check` + `mypy anvil` | Any one failing fails the target. |
| `fmt` | `ruff format` + `ruff check --fix` | Autofix. |
| `down` | `docker compose down` | Stops containers, keeps the volume. |
| `clean` | `docker compose down -v` | Stops containers **and deletes `anvil-pgdata`.** Every migrated table goes with it. |

---

## 6. Database

### 6.1 Migrations

Alembic runs synchronously against psycopg while the application uses asyncpg — `alembic/env.py`
sets `sqlalchemy.url` from `Settings.sync_database_url`, which rewrites `+asyncpg` to `+psycopg`.
Migrations are a deploy-time tool; there is nothing to gain from making them async.

`alembic.ini` sets `script_location = alembic`, `prepend_sys_path = .` (so `anvil` is importable)
and `timezone = UTC`. Logging is configured there too: root and SQLAlchemy at `WARNING`, Alembic at
`INFO`, all to stderr.

Two revisions:

| Revision | What it creates |
|---|---|
| `8c4dce6e89c7` | Initial schema — 33 tables, with named constraints and indexes. `alembic/env.py` imports `anvil.db.models` to register them all on `Base.metadata`. |
| `9a1b2c3d4e5f` (head) | The ledger immutability guard. Installs `anvil_reject_ledger_mutation()` and `UPDATE`/`DELETE` triggers on `ledger_entries`, `ledger_transactions`, `domain_events` and `audit_records`. |

```bash
.venv/bin/alembic current                  # what the database is at
.venv/bin/alembic heads                    # what the code expects: 9a1b2c3d4e5f
.venv/bin/alembic history --verbose
.venv/bin/alembic upgrade head
.venv/bin/alembic downgrade -1             # drops the immutability triggers
.venv/bin/alembic revision --autogenerate -m "what changed"
```

`compare_type` and `compare_server_default` are both on, so autogenerate notices column type and
default drift, not only added and dropped tables. Always read the generated file before committing.

### 6.2 Verifying the append-only guard

After `upgrade head`, the guard is a database refusal, not an application convention:

```
psql> UPDATE ledger_entries SET amount_minor = 999999999 WHERE id = 'len_...';
ERROR:  ledger is append-only: UPDATE on ledger_entries is refused. Post a reversal instead.
HINT:   Corrections are made by posting a mirrored REVERSAL transaction that
        references the original, never by editing it.
```

### 6.3 Seeding

There is no database seed. `make seed` targets `anvil.simulator.seed`, which does not exist.

Every seeded world is built **in process** from `build_population(seed=..., size=...)`:

- The API builds one at startup from `ANVIL_SEED` with `size=900` (§4.4).
- `anvil.evidence.run_batch` builds one per invocation from `--seed` and `--size`.
- `/api/batch` builds and caches one per `(seed, size, with_model)`.

Nothing is persisted, so nothing needs seeding. Reproducibility comes from the seed and the fixed
epoch, not from a database fixture.

### 6.4 The batch experiment

```bash
make batch                                 # SEED=20260902 SIZE=3000
make batch SEED=42 SIZE=5000
make batch-with-model
.venv/bin/python -m anvil.evidence.run_batch --seed 20260902 --size 3000 \
    --split even --horizon-days 30 --json artifacts/batch.json
```

| Flag | Default | Meaning |
|---|---|---|
| `--seed` | `20260902` | Reproduces a run byte for byte. |
| `--size` | `3000` | Subscriptions in the book. |
| `--split` | `even` | `even` gives each arm a third, which is what makes the confidence intervals tight enough to conclude anything. `production` holds back 10% for control and 10% for baseline. |
| `--with-model` | off | Model the LLM classifier as available. |
| `--horizon-days` | `30` | Recovery horizon. |
| `--json PATH` | — | Also write the machine-readable summary. |

The runner reconfigures structlog to `ERROR` for the duration: the degradation warnings each case
emits are expected behaviour, and at batch scale they would bury the report.

---

## 7. Logging

### 7.1 Setup

`anvil/core/logging.py` configures structlog once, from `configure_logging()`, called by the API
lifespan. Output goes to **stderr** through a `PrintLoggerFactory` — not through the stdlib
handlers — and loggers are cached on first use.

The processor chain, in order:

1. `merge_contextvars` — anything bound with `structlog.contextvars` for the current task.
2. `add_log_level`.
3. `TimeStamper(fmt="iso", utc=True)`.
4. `StackInfoRenderer`.
5. **`redact_processor`** — §7.3.
6. `format_exc_info`.
7. `JSONRenderer` or `ConsoleRenderer`, per `ANVIL_LOG_FORMAT`.

`httpx`, `httpcore`, `asyncio` and `aiosqlite` are pinned to `WARNING`.

### 7.2 Reading the fields

Every event carries the same four fields, plus whatever the call site binds:

| Field | Source | Meaning |
|---|---|---|
| `event` | first positional argument | The event name. Always a stable snake-case identifier, never a sentence — `debit_attempted`, `anvil_api_ready`, `planner_unavailable`. Grep on this. |
| `level` | `add_log_level` | `debug` / `info` / `warning` / `error`. |
| `timestamp` | `TimeStamper` | ISO-8601, UTC, with microseconds. |
| `logger` | `get_logger(__name__)` binds it | The module that emitted the line. `structlog.stdlib.add_logger_name` is deliberately not used: it reads `logger.name`, which only exists on a stdlib logger, and this configuration uses a `PrintLogger`. |

Recurring bound fields worth knowing: `case_id`, `correlation_id`, `amount_minor` (always integer
paise), `code` (an error code from §8), `path`, `mode`, `seed`.

`console` format, coloured only when stderr is a TTY:

```
2026-09-03T05:08:12.578846Z [info     ] debit_attempted   [anvil.demo] amount_minor=149900 case_id=cse_01J vpa=ro***nk
```

`json` format, one object per line:

```json
{"logger": "anvil.demo", "case_id": "cse_01J", "amount_minor": 149900, "vpa": "ro***nk", "event": "debit_attempted", "level": "info", "timestamp": "2026-09-03T05:08:12.677275Z"}
```

Because output is on stderr, redirect it explicitly:

```bash
ANVIL_LOG_FORMAT=json .venv/bin/uvicorn anvil.main_api:app 2>&1 | jq -c 'select(.level=="warning")'
```

### 7.3 Redaction

`redact_processor` masks the value of any key in `SENSITIVE_KEYS`, matched case-insensitively,
before the renderer sees it. Masking keeps two characters at each end (`ro***nk`); anything four
characters or shorter becomes `***`.

The keys: `vpa`, `upi_id`, `card_number`, `pan`, `account_number`, `ifsc`, `phone`, `mobile`,
`email`, `customer_email`, `customer_phone`, `api_key`, `key_secret`, `webhook_secret`,
`authorization`, `anthropic_api_key`, `razorpay_key_secret`, `token`, `otp`, `mpin`, `password`,
`secret`, `signature`.

Two operational caveats:

- **Top-level keys only.** The processor walks `event_dict` one level deep. A payment identifier
  nested inside a dict you log as one field is not masked. Bind sensitive values as their own
  keyword argument, or redact before logging.
- **Redaction happens on write, not on read.** A log aggregator never receives the raw value, which
  also means it cannot be recovered from the logs later.

Tests and the batch runner reconfigure structlog to `ERROR`. That is intentional and is scoped to
those entry points; production logging is untouched.

---

## 8. Error taxonomy

`anvil/core/errors.py` defines every failure Anvil raises. Each class carries a stable `code`, an
`http_status`, and a `retryable` flag — so retry behaviour is a property of the error type rather
than a judgement call at each call site.

The API translates them once, in `anvil/api/app.py`:

```json
{"error": {"code": "policy_denied", "message": "...", "retryable": false, "context": {}}}
```

and logs `anvil_error` at `warning` with `code`, `path` and `message`. FastAPI's own
`HTTPException`, raised for plain 404s in the operations router, produces the standard
`{"detail": "..."}` shape instead; the console handles both.

### 8.1 Invariant violations — the system is wrong

These abort the transaction and page a human. They are never caught and handled: a system that
silently recovers from an unbalanced ledger is a system that silently loses money.

| Class | `code` | HTTP | Retryable | Operationally |
|---|---|---|---|---|
| `InvariantViolation` | `invariant_violation` | 500 | no | A rule from ARCHITECTURE §6 broke. Stop and investigate. |
| `UnbalancedTransaction` | `unbalanced_transaction` | 500 | no | Debits did not equal credits. Nothing was written. |
| `LedgerImmutabilityViolation` | `ledger_immutability_violation` | 500 | no | Something attempted `UPDATE`/`DELETE` on an append-only table. Post a reversal instead. |

### 8.2 Domain refusals — the system is right and is saying no

| Class | `code` | HTTP | Retryable | Operationally |
|---|---|---|---|---|
| `DomainError` | `domain_error` | 422 | no | Base for the refusals below. |
| `AuthorisationDenied` | `authorisation_denied` | 403 | no | No valid mandate backs this debit. Fails closed; retrying changes nothing. |
| `StepUpRequired` | `step_up_required` | 401 | no | The action exceeds the delegated cap. The customer must re-authenticate (AFA); the graph parks on a durable interrupt. |
| `PolicyDenied` | `policy_denied` | 403 | no | No rule permitted the action. A *gap* in policy denies rather than allows. |
| `BudgetExhausted` | `budget_exhausted` | 409 | no | The concession budget reservation failed. The planner is re-invoked with concessions removed from its space. |
| `ConsentMissing` | `consent_missing` | 403 | no | No consent for this specific purpose. The suppression is persisted with its reason. |
| `StoppingRuleFired` | `stopping_rule_fired` | 409 | no | Contact pressure or attempt limits ended the campaign. Not an error to be worked around. |
| `InsufficientReservation` | `insufficient_reservation` | 409 | no | The reserved amount does not cover the posting. |
| `ModelProposedOutOfBounds` | `model_proposed_out_of_bounds` | 422 | no | The model proposed an action outside the closed action space or beyond its limits. Refused before execution and counted as a **model-safety event** on the dashboard. A rising count is a signal, not noise. |

### 8.3 Conflicts

| Class | `code` | HTTP | Retryable | Operationally |
|---|---|---|---|---|
| `ConflictError` | `conflict` | 409 | no | Base. |
| `OptimisticLockConflict` | `optimistic_lock_conflict` | 409 | no | Two operators resolved the same approval. The second sees a refreshed view; nothing is overwritten. |
| `DuplicateEvent` | `duplicate_event` | **200** | no | A webhook already processed. At-least-once delivery working correctly — answered 200, no business logic re-run. |
| `StaleEvent` | `stale_event` | **200** | no | An out-of-order webhook older than the state held. Recorded with its reason, then discarded. Acknowledged, because the delivery was valid; it simply changes nothing. |

`DuplicateEvent` and `StaleEvent` returning 200 is deliberate. Answering an error would teach
Razorpay's delivery system to retry forever.

### 8.4 External boundaries

| Class | `code` | HTTP | Retryable | Operationally |
|---|---|---|---|---|
| `ExternalError` | `external_error` | 502 | **yes** | Base for boundary failures. Back off and retry. |
| `GatewayError` | `gateway_error` | 502 | **yes** | Razorpay refused or errored, and said so. |
| `GatewayTimeout` | `gateway_timeout` | 504 | **no** | The outcome is genuinely unknown. **Never blind-retry.** The case parks in `PENDING_RECONCILIATION`, nothing is written off, and the reconciler re-asks using the same idempotency key. |
| `LLMError` | `llm_error` | 502 | **yes** | Model call failed. The deterministic classifier and a conservative plan take over; recovery continues with reduced sophistication. |
| `LLMTimeout` | `llm_timeout` | 502 | **yes** | As above, on a timeout. |
| `LLMRateLimited` | `llm_rate_limited` | 502 | **yes** | Back off; the fallback path still works. |
| `StructuredOutputInvalid` | `structured_output_invalid` | 502 | **yes** | The model returned something the schema rejects. Retry with the validation error appended. Never a partial write. |
| `WebhookVerificationFailed` | `webhook_verification_failed` | 400 | no | The signature does not match the raw body. See §9. |
| `WebhookReplayRejected` | `webhook_replay_rejected` | 400 | no | Timestamp outside the replay window. Clock skew or an attack; both deserve a 400. |
| `FixtureMissing` | `fixture_missing` | 500 | no | Offline mode has no recorded response for this call. Add the fixture under `ANVIL_FIXTURES_DIR`. |

### 8.5 Lookup

| Class | `code` | HTTP | Retryable |
|---|---|---|---|
| `NotFound` | `not_found` | 404 | no |
| `ValidationError` | `validation_error` | 400 | no |

`GatewayTimeout` is the one to internalise: it inherits from `ExternalError` but explicitly sets
`retryable = False`. Everything else at the boundary is safe to retry; a timed-out debit is not.

---

## 9. Webhooks and the gateway boundary

### 9.1 Configuration

| Setting | Effect |
|---|---|
| `ANVIL_RAZORPAY_WEBHOOK_SECRET` | The HMAC-SHA256 key. Empty ⇒ every delivery is rejected. |
| `ANVIL_WEBHOOK_TOLERANCE_SECONDS` | Symmetric freshness window, default 300s. |

Headers, all matched case-insensitively:

| Header | Required | Use |
|---|---|---|
| `X-Razorpay-Signature` | yes | Hex HMAC-SHA256 over the exact bytes delivered. |
| `X-Razorpay-Event-Id` | yes | The dedupe key. A delivery without it is a `ValidationError` — without it the delivery cannot be deduplicated. |
| `X-Razorpay-Account-Id` | no | Falls back to `account_id` in the body. |

### 9.2 The four steps, in the one order that is correct

`anvil/gateway/webhooks.py` implements them as four independently testable functions:

1. **`verify_signature`** — constant-time `hmac.compare_digest` over the **raw bytes**, before any
   parsing. Failure → `WebhookVerificationFailed`, 400. The function takes `bytes`, not a dict, and
   the type signature refuses a parsed body: re-serialising JSON produces a different byte sequence
   (key order, whitespace, unicode escaping, float formatting are all serialiser choices), so a
   signature checked against a re-serialised body can never match.
2. **`check_replay_window`** — a valid signature proves authorship, not freshness. The payload
   timestamp is inside the signed bytes, so it is a usable freshness bound. Failure →
   `WebhookReplayRejected`, 400.
3. **`claim_event`** — insert into `processed_webhooks` inside a `SAVEPOINT` and catch Postgres
   SQLSTATE `23505`. Not a `SELECT` first: that is a race. The unique index is the only atomic
   thing, so the exception is the answer, not an error path. Collision → `DuplicateEvent`, 200,
   with no business logic run. Narrowed to `23505` specifically — a foreign-key violation is a bug
   and must not be answered with 200. The SHA-256 of the delivered body is stored alongside, so a
   replay of a known event id with a *mutated* body is detectable after the fact.
4. **`check_ordering` / `require_in_order`** — run by the caller holding the domain transaction,
   because it needs the aggregate's current version. Stale → `StaleEvent`, 200, recorded and
   discarded.

`ingest()` runs steps 1–3. Step 4 is called separately.

Ordering uses two signals. Where a lifecycle provably cannot run backwards, a lower-ranked state is
stale regardless of timestamp:

| Entity | Ranked states |
|---|---|
| `payment` | `created` 0, `authorized` 1, `failed` 2, `captured` 3, `refunded` 4 |
| `order` | `created` 0, `attempted` 1, `paid` 2 |
| `refund` | `created` 0, `pending` 1, `failed` 2, `processed` 2 |

`subscription` and `token` are **deliberately absent**. A subscription legitimately oscillates
(`pending` back to `active` the moment a recovery attempt settles) and a token moves `paused` →
`confirmed` when a customer resumes it. Ranking those would discard exactly the events Anvil needs.
For them the event timestamp is the only monotonic quantity we are entitled to trust — and equal
timestamps are allowed through, because Razorpay's timestamps have one-second resolution and two
genuine transitions can share a second.

### 9.3 Event contracts

`anvil/gateway/events.py` reduces every delivery to one `NormalisedEvent` — entity, aggregate id,
money, failure fields, timestamp — so nothing downstream ever sees a raw webhook dict.

The 23 recognised event types:

```
payment.authorized      payment.captured        payment.failed
order.paid              invoice.paid            invoice.expired
refund.processed        refund.failed
subscription.authenticated  subscription.activated  subscription.charged
subscription.pending    subscription.halted     subscription.cancelled
subscription.paused     subscription.resumed    subscription.completed
token.confirmed         token.rejected          token.paused
token.cancelled         token.expired
```

**Unknown event types are accepted, never fatal.** An unrecognised `event` is parsed best-effort,
marked `recognised=False`, recorded in `processed_webhooks`, and acknowledged — but the graph never
sees it. A 500 on an unmodelled event teaches Razorpay's delivery system to retry forever and hides
the real events behind a wall of failures.

Facts to know when reading events:

- `subscription.charged` reports the **payment's** amount, since that is what settled. A halted
  subscription reports no amount at all; inventing one from the plan would put a figure in the
  ledger's line of sight that nobody debited.
- `order.paid` reports the **order's** amount, not the payment's.
- `token.*` reports `recurring_details.status`, not the token's own `status`: a token can be alive
  while its recurring authority is rejected.
- The mandate reference is probed across `umn`, `mrn`, `upi.umn`, `recurring_details.umn`,
  `bank_details.umrn` and `acquirer_data.umn`. UPI Autopay, e-NACH and card rails all put the same
  fact somewhere different.
- Failure classification prefers `error_reason` over `error_code`: `GATEWAY_ERROR` is a bucket that
  would collapse an issuer outage and a revoked mandate into one posture.
- `NormalisedEvent.audit_payload()` is the persistence-safe projection. `raw` must never reach the
  audit log. PII keys (`email`, `contact`, `vpa`, `customer_email`, `customer_contact`, `card`,
  `bank_details`) are excluded on the way in, not filtered on read.

### 9.4 Outbound contracts

`anvil/gateway/contracts.py` defines the `RazorpayGateway` Protocol that both the live client and
the offline adapter satisfy structurally, so the executor, reconciler and graph run identical code
in both modes. Operationally relevant guarantees:

- **Every mutating method takes a caller-supplied `idempotency_key`.** There is no signature that
  permits moving money without one.
- The key is written into Razorpay's `receipt` (orders) or `reference_id` (payment links), giving
  the reconciler a server-side handle on an object whose creation response was lost.
  `MAX_REFERENCE_LENGTH` is 40 — Razorpay's cap — and exceeding it is a hard `ValidationError`,
  not a silent truncation and a mystery reconciliation miss.
- `coerce_minor` rejects `float` and `bool`. A gateway that started sending `1499.0` is a change to
  notice loudly, not absorb.
- `DEFAULT_BASE_URL` is `https://api.razorpay.com/v1`, overridable so a sandbox or test points
  elsewhere.

The live HTTP client, the offline adapter and the reconciler are not implemented yet.

---

## 10. Tests and lint

```bash
make test                                  # 203 unit tests, no database
make invariants                            # the 8 financial invariant tests
make test-all                              # brings up Docker Postgres first
make lint                                  # ruff check + ruff format --check + mypy anvil
make fmt                                   # autofix
```

Markers are declared in `pyproject.toml` and `--strict-markers` is on, so a typo in a marker is an
error rather than a silently skipped test:

| Marker | Meaning |
|---|---|
| `integration` | Requires a live Postgres. |
| `e2e` | Full stack end to end. |
| `invariant` | Enforces a financial invariant from ARCHITECTURE §6. |

`tests/integration/` and `tests/e2e/` are currently empty packages.

mypy runs in `strict` mode with the pydantic plugin, `warn_unreachable` and
`disallow_any_generics`. Ruff runs at line length 100 with `E,F,I,N,UP,B,A,C4,SIM,RUF,ASYNC,S,DTZ,T20`
selected; the per-file ignores are documented in `pyproject.toml` (seeded randomness in the
simulator, printing in `run_batch`, no `S`/`T20` in tests).

---

## 11. Troubleshooting

### `make: .venv/bin/python: No such file or directory`

The virtualenv is missing. `make venv`. Every target hard-codes `.venv/bin/...` — there is no
fallback to a system interpreter and no `activate` step.

### `ModuleNotFoundError: No module named 'anvil'`

You are running a system Python, or the editable install did not complete. Use `.venv/bin/python`
explicitly, or re-run `make venv`. Note `scripts/tour.py` inserts the repository root on `sys.path`
itself, so the tour can run before the package is installed — nothing else can.

### Postgres is not running

```bash
pg_isready -h 127.0.0.1                    # expect: 127.0.0.1:5432 - accepting connections
brew services start postgresql@16
brew services list | grep postgres
```

Symptoms: `alembic upgrade head` fails with `connection refused`. The API, the tour and the batch
are unaffected — none of them connect to a database.

### `FATAL: role "anvil" does not exist` / `database "anvil" does not exist`

The default `ANVIL_DATABASE_URL` expects a role and database both named `anvil`. Either create them:

```bash
createuser -h 127.0.0.1 -s anvil
createdb  -h 127.0.0.1 -O anvil anvil
```

or point the URL at what you have:

```bash
export ANVIL_DATABASE_URL="postgresql+asyncpg://$(whoami)@127.0.0.1:5432/anvil"
```

A Homebrew Postgres normally trusts local connections, so the `:anvil` password in the default URL
is ignored. The compose Postgres genuinely uses it.

### Migrations out of date

```bash
.venv/bin/alembic current                  # what the database has
.venv/bin/alembic heads                    # what the code expects: 9a1b2c3d4e5f
```

If they differ, `.venv/bin/alembic upgrade head`. `Target database is not up to date` from
`revision --autogenerate` means the same thing: upgrade before generating.

If `current` is empty against a database that already has tables, you are pointed at the wrong
database — check `ANVIL_DATABASE_URL` and whether a `.env` in the working directory is overriding
your shell.

### Port conflicts

| Port | Used by |
|---|---|
| 8000 | uvicorn / the `api` container |
| 5432 | Homebrew Postgres **and** the `postgres` container |
| 6379 | Redis container |
| 3000 | The removed Next.js console; the compose file still maps it |

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

The common one is 5432: `make db-up` fails with `port is already allocated` when Homebrew Postgres
is running. Either `brew services stop postgresql@16`, or stay on the native path and skip the
Docker targets entirely. Running both against the same port silently sends migrations to whichever
one won.

For 8000, run uvicorn elsewhere: `.venv/bin/uvicorn anvil.main_api:app --port 8010`.

### No LLM credentials

This is the normal state. Offline is the default, needs no keys, and `/health` reports
`"model": "offline fixtures"`. The tour, the tests, the batch and the console all run with no
Razorpay or Anthropic credentials and no network.

If you set `ANVIL_MODE=live` without all four credentials, `Settings` raises at startup and names
the missing ones (§3.3). Unset `ANVIL_MODE` to go back to offline. Setting `ANVIL_ANTHROPIC_API_KEY`
alone does not enable anything — the Anthropic client is not implemented yet.

Where a model call would be made but cannot be, the graph degrades rather than stops: the
deterministic classifier and a conservative plan take over, `degraded` is set on the state, and the
run logs `diagnosis_unavailable` / `planner_unavailable` at `warning`. Those lines are expected
behaviour, not a fault.

### `make seed` fails with `No module named anvil.simulator.seed`

Correct — the module does not exist and there is no database seeding step. See §6.3. Ignore the
target; the seeded world is built in process from `ANVIL_SEED`.

### `make demo` fails

Three separate reasons: `seed` (above), the `worker` service (`anvil/main_worker.py` does not
exist) and the `console` service (`console/` was removed). Use §4.1.

### `make lint` reports errors

At the time of writing `ruff check` reports 4 errors and `ruff format --check` wants to reformat one
file, all in the uncommitted API work. `make fmt` fixes the formatting and the autofixable lint;
the rest need a look. `make lint` runs three tools and fails on the first, so re-run it after each
fix to see what is left.

### A configuration change had no effect

Three possibilities, in order of likelihood:

1. `get_settings()` is `lru_cache`d and `Settings` is frozen — restart the process.
2. The `.env` file is read from the **current working directory**. Run from the repository root.
3. `extra="ignore"` means an unrecognised key is discarded silently. Check the spelling and the
   `ANVIL_` prefix, then confirm via `/health` or a log line.

### Logs are empty when piping

structlog writes to stderr, not stdout. Use `2>&1` before the pipe, or `2>` to a file.

### Webhook signature always fails

In order: is `ANVIL_RAZORPAY_WEBHOOK_SECRET` set (an empty secret rejects everything by design); is
the signature computed over the **raw request bytes** rather than a re-serialised body; is
`X-Razorpay-Event-Id` present. A missing event id raises `ValidationError`, not a signature failure
— the message says so explicitly.

### The console renders but has no data

The API builds its world at startup. If `anvil_api_ready` logged `at_risk_cases=0` or
`pending_approvals=0`, the seed produced no workable cases — restore `ANVIL_SEED=20260902`. If the
page loads but every panel errors, you are serving the static file without the API behind it; the
console is same-origin and has no configurable base URL.
