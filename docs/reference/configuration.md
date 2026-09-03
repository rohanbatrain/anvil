# Configuration

Every setting, its default and what it does. All are read from the environment
with an `ANVIL_` prefix, or from a `.env` file in the working directory.

**Every value has a working default.** Offline mode — the default — needs no
credentials at all, which is what lets a clone be run without ever handling one.

This page is generated from `anvil/core/config.py`, so it cannot drift from the
model. Settings are held frozen and secrets are `SecretStr`, so they do not
appear in a traceback or a log line.

## Runtime

| Variable | Default | Notes |
|---|---|---|
| `ANVIL_MODE` | `offline` | offline needs no credentials at all; live requires all four below. |
| `ANVIL_ENV` | `local` | Where this process is running. |
| `ANVIL_LOG_LEVEL` | `INFO` | Minimum level rendered. |
| `ANVIL_LOG_FORMAT` | `console` | json for aggregation, console for reading. |
| `ANVIL_SEED` | `20260902` | Every stochastic component derives from this. Same seed, same batch. |

## Infrastructure

| Variable | Default | Notes |
|---|---|---|
| `ANVIL_DATABASE_URL` | `postgresql+asyncpg://anvil:anvil@localhos…` | Needed only for migrations and integration tests. |
| `ANVIL_REDIS_URL` | `redis://localhost:6379/0` | Reserved; not yet required. |
| `ANVIL_DB_POOL_SIZE` | `20` | Connections held open per process. |
| `ANVIL_DB_MAX_OVERFLOW` | `10` | Extra connections under burst. |
| `ANVIL_DB_STATEMENT_TIMEOUT_MS` | `15000` | Server-side cap, so a runaway query cannot hold a worker. |

## Live-mode credentials

| Variable | Default | Notes |
|---|---|---|
| `ANVIL_RAZORPAY_KEY_ID` | `` | Test-mode key id, rzp_test_... |
| `ANVIL_RAZORPAY_KEY_SECRET` | `(unset)` | Shown once by the dashboard. |
| `ANVIL_RAZORPAY_WEBHOOK_SECRET` | `(unset)` | Signs inbound webhooks; you choose this value. |
| `ANVIL_ANTHROPIC_API_KEY` | `(unset)` | Unset in offline mode; fixtures are used instead. |

## Models

| Variable | Default | Notes |
|---|---|---|
| `ANVIL_MODEL_PLANNER` | `claude-opus-5` | Planning: judgement under a live budget. |
| `ANVIL_MODEL_CLASSIFIER` | `claude-sonnet-5` | High-volume classification of unmapped codes. |
| `ANVIL_MODEL_COMPOSER` | `claude-sonnet-5` | Customer-facing copy, per language and cause. |

## Guardrails

| Variable | Default | Notes |
|---|---|---|
| `ANVIL_WEBHOOK_TOLERANCE_SECONDS` | `300` | Replay window. A payload older than this is rejected. |
| `ANVIL_LLM_MAX_RETRIES` | `3` | Retries on malformed structured output, with the error appended. |
| `ANVIL_LLM_TIMEOUT_SECONDS` | `60` | Per model call. |
| `ANVIL_LLM_MAX_OUTPUT_TOKENS` | `4096` | Ceiling per model call. |

## Paths

| Variable | Default | Notes |
|---|---|---|
| `ANVIL_FIXTURES_DIR` | `anvil/llm/fixtures` | Recorded model responses used in offline mode. |

## Validation at startup

`ANVIL_MODE=live` **fails fast** if any of the four live-mode credentials is
missing, naming exactly which ones. Discovering a missing webhook secret on the
first inbound webhook is worse than not starting.

`ANVIL_SEED` must be positive. A seed of zero is still reproducible but reads as
"unset" to anyone auditing a batch.

## Derived values

`sync_database_url` rewrites the asyncpg URL for psycopg, which Alembic and the
LangGraph Postgres checkpointer need. `raw_database_url` strips the driver for
libraries that build their own connection. Neither is set directly — configure
`ANVIL_DATABASE_URL` and the rest follows.
