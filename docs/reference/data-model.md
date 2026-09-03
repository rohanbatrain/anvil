# Data model

Every table, every column, every constraint, and the migration workflow.

Anvil's schema is defined in SQLAlchemy 2.0 declarative models under `anvil/db/models/` and realised in
Postgres by two Alembic migrations. The models are the source of truth; the migrations are generated
from them and reviewed by hand. Thirty-two tables, one database, no sharding, no secondary store.

This document describes what the code and the migrations actually define. Where an invariant is
enforced by the database rather than by convention, it says so and names the mechanism.

---

## 1. Where the schema lives

| Path | Contents |
|---|---|
| `anvil/db/base.py` | Declarative `Base`, naming convention, custom column types, mixins |
| `anvil/db/session.py` | Async engine, sessionmaker, `session_scope()`, the FastAPI session dependency |
| `anvil/db/models/parties.py` | `merchants`, `customers`, `plans`, `subscriptions` |
| `anvil/db/models/authorisation.py` | `authorisations`, `authorisation_usages`, `step_up_challenges` |
| `anvil/db/models/recovery.py` | `recovery_cases`, `recovery_actions`, `payment_attempts` |
| `anvil/db/models/ledger.py` | `accounts`, `ledger_transactions`, `ledger_entries`, `budget_reservations`, `concession_budgets` |
| `anvil/db/models/policy.py` | `policy_bundles`, `policy_rules`, `policy_evaluations`, `approvals` |
| `anvil/db/models/comms.py` | `consent_receipts`, `outreach_messages`, `erasure_requests`, `contact_ledger` |
| `anvil/db/models/experiment.py` | `recovery_batches`, `arm_assignments`, `batch_results` |
| `anvil/db/models/platform.py` | `domain_events`, `outbox`, `audit_records`, `processed_webhooks`, `idempotency_records`, `llm_calls` |
| `anvil/db/models/__init__.py` | Re-exports every model, so importing the package registers the whole schema on `Base.metadata` |
| `anvil/domain/enums.py` | Every enumerated vocabulary used by a column |
| `anvil/domain/money.py` | `Money` and `Currency` |
| `anvil/ledger/immutability.py` | The append-only trigger DDL |
| `alembic/versions/` | `8c4dce6e89c7` (initial schema) then `9a1b2c3d4e5f` (immutability guard) |

Importing `anvil.db.models` is what makes `Base.metadata` complete. Alembic, the test fixtures and the
application all import the package rather than individual modules, so no code path can see a partial
schema. A new model that is not re-exported from `anvil/db/models/__init__.py` is invisible to
autogenerate, which will then propose to drop its table.

---

## 2. Connection and session management

### Configuration

Settings come from `anvil/core/config.py`, a frozen pydantic-settings model with the `ANVIL_` env
prefix and an `.env` file. Every value has a working default.

| Setting | Env var | Default | Meaning |
|---|---|---|---|
| `database_url` | `ANVIL_DATABASE_URL` | `postgresql+asyncpg://anvil:anvil@localhost:5432/anvil` | Async URL used by the application engine |
| `db_pool_size` | `ANVIL_DB_POOL_SIZE` | `20` | Persistent connections per process |
| `db_max_overflow` | `ANVIL_DB_MAX_OVERFLOW` | `10` | Additional connections above the pool size |
| `db_statement_timeout_ms` | `ANVIL_DB_STATEMENT_TIMEOUT_MS` | `15000` | Passed to Postgres as `statement_timeout` |
| `redis_url` | `ANVIL_REDIS_URL` | `redis://localhost:6379/0` | Not part of the relational schema |

Two derived URLs exist because two drivers are in play:

| Property | Transformation | Used by |
|---|---|---|
| `sync_database_url` | `+asyncpg` replaced with `+psycopg` | Alembic, the LangGraph Postgres checkpointer |
| `raw_database_url` | driver suffix stripped entirely | Libraries that build their own connection |

### Engine

`anvil/db/session.py` creates one engine per process, on startup, disposed on shutdown through the
FastAPI lifespan. `create_async_engine` is called with `pool_pre_ping=True`, `pool_recycle=1800`,
`echo=False`, and `connect_args` setting the Postgres session parameters `application_name = anvil`
and `statement_timeout` from the configured milliseconds.

`init_engine()` is idempotent. The sessionmaker is built with `expire_on_commit=False` and
`autoflush=False`. Sessions are short-lived and never shared across tasks.

`session_scope()` is the transaction boundary: it commits on success and rolls back on any exception.
Everything that must be atomic — a ledger posting and its event, a state change and its outbox entry —
happens inside one. `get_session()` wraps it as a FastAPI dependency, one session per request.

### Local Postgres

`docker-compose.yml` provisions `postgres:16-alpine` as the `postgres` service: user, password and
database all `anvil`, port `5432` published, initialised with `--data-checksums`, running with
`max_connections=200`, `shared_buffers=256MB` and `log_min_duration_statement=500`. Data lives in the
named volume `anvil-pgdata`. The healthcheck is `pg_isready -U anvil -d anvil` every three seconds.

Containers reach the database at `postgresql+asyncpg://anvil:anvil@postgres:5432/anvil`; processes on
the host use the `localhost` default. Nothing in the schema depends on running Postgres in Docker — a
local server with the same database name, role and password works identically.

---

## 3. Conventions

### Constraint naming

`Base.metadata` carries an explicit naming convention so that autogenerate produces stable, reviewable
names and no constraint is ever anonymous.

| Kind | Template | Example |
|---|---|---|
| Index | `ix_%(column_0_N_label)s` | `ix_recovery_cases_status` |
| Unique | `uq_%(table_name)s_%(column_0_N_name)s` | `uq_merchants_razorpay_account_id` |
| Check | `ck_%(table_name)s_%(constraint_name)s` | `ck_ledger_entries_entry_amount_strictly_positive` |
| Foreign key | `fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s` | `fk_customers_merchant_id_merchants` |
| Primary key | `pk_%(table_name)s` | `pk_ledger_entries` |

Constraints declared with an explicit `name=` in `__table_args__` keep that name verbatim for unique
constraints and indexes (`uq_action_case_sequence`, `ix_cases_due`); check constraints have their
declared name substituted into the `ck_` template, which is why `name="quiet_start_range"` on
`merchants` becomes `ck_merchants_quiet_start_range`.

### Column types

| Helper | Underlying type | Behaviour |
|---|---|---|
| `UTCDateTime` | `TIMESTAMP WITH TIME ZONE` | Raises `ValueError` on a naive datetime at bind time; returns UTC-aware datetimes on read |
| `CurrencyType` | `VARCHAR(3)` | Stores `Currency.value` (`INR`, `USD`); reads back as a `Currency` |
| `pk_column(prefix)` | `VARCHAR(32)`, primary key | Carries a column comment naming the id prefix, e.g. `prefixed ULID, e.g. cse_01J...` |
| `money_minor()` | `BIGINT NOT NULL` | A signed integer count of minor units. No default: the application must supply a value |
| `sa.Enum(..., native_enum=False, length=n)` | `VARCHAR(n)` | See [section 5](#5-enumerated-types) |

`Base.type_annotation_map` maps `dict[str, Any]`, `list[str]` and `list[dict[str, Any]]` annotations to
`JSONB`, and bare `datetime` annotations to `TIMESTAMP WITH TIME ZONE`.

There is no `FLOAT`, `REAL`, `NUMERIC` or `DECIMAL` column anywhere in the schema. Money is a composite
of an integer minor-unit count and a currency code, which is what makes invariant 3 structural rather
than advisory.

### Mixins

| Mixin | Adds | Applied to |
|---|---|---|
| `TimestampMixin` | `created_at` (indexed, `DEFAULT now()`), `updated_at` (`DEFAULT now()`, `onupdate=now()`) | 21 tables |
| `CreatedAtMixin` | `created_at` only (indexed, `DEFAULT now()`) | The 11 append-only tables |
| `MerchantScopedMixin` | `merchant_id VARCHAR(32) NOT NULL`, indexed, FK to `merchants.id` `ON DELETE RESTRICT` | 20 tables |

`VersionMixin` and `versioned_mapper_args()` are defined in `anvil/db/base.py` but no model uses them.
`approvals` declares its own `version` column and `__mapper_args__ = {"version_id_col": version}`
directly; it is the only table with optimistic locking. Likewise `currency_col()` is defined but
unused — every currency column is declared inline as
`mapped_column(CurrencyType, nullable=False, default=Currency.INR)`, which means the default is applied
by the ORM on insert and there is no DDL-level `DEFAULT` on any currency column.

### Reading the column tables below

- **Type** is the Postgres type the migration creates.
- **Null** is `yes` where the column is nullable.
- **Default** is `now()` where it is a DDL `DEFAULT`, `identity` for a generated column, and otherwise
  the ORM-side default applied on insert. `—` means no default: the application must supply a value.
- Enum defaults are shown as the value stored in the column, which is the member name in upper case.

---

## 4. Entity-relationship overview

Anvil is single-database and multi-tenant by column: twenty of the thirty-two tables carry
`merchant_id` and every query filters on it. `merchants` is the root of the tenancy graph, `customers`
and `subscriptions` describe the world being recovered for, `recovery_cases` is the unit of work, and
the ledger, policy and platform tables hang off those.

```mermaid
erDiagram
    merchants ||--o{ customers : merchant_id
    merchants ||--o{ plans : merchant_id
    merchants ||--o{ subscriptions : merchant_id
    merchants ||--o{ authorisations : merchant_id
    merchants ||--o{ step_up_challenges : merchant_id
    merchants ||--o{ recovery_cases : merchant_id
    merchants ||--o{ recovery_actions : merchant_id
    merchants ||--o{ payment_attempts : merchant_id
    merchants ||--o{ accounts : merchant_id
    merchants ||--o{ ledger_transactions : merchant_id
    merchants ||--o{ concession_budgets : merchant_id
    merchants ||--o{ budget_reservations : merchant_id
    merchants ||--o{ policy_bundles : merchant_id
    merchants ||--o{ policy_evaluations : merchant_id
    merchants ||--o{ approvals : merchant_id
    merchants ||--o{ consent_receipts : merchant_id
    merchants ||--o{ outreach_messages : merchant_id
    merchants ||--o{ erasure_requests : merchant_id
    merchants ||--o{ contact_ledger : merchant_id
    merchants ||--o{ recovery_batches : merchant_id

    customers ||--o{ subscriptions : customer_id
    customers ||--o{ authorisations : customer_id
    customers ||--o{ recovery_cases : customer_id
    customers ||--o{ step_up_challenges : customer_id
    customers ||--o{ consent_receipts : customer_id
    customers ||--o{ outreach_messages : customer_id
    customers ||--o{ erasure_requests : customer_id
    customers |o--o{ accounts : customer_id

    plans ||--o{ subscriptions : plan_id
    subscriptions ||--o{ recovery_cases : subscription_id
    subscriptions |o--o{ authorisations : subscription_id

    authorisations ||--o{ authorisation_usages : "authorisation_id CASCADE"
    authorisations ||--o{ step_up_challenges : authorisation_id
    authorisations |o--o{ recovery_cases : authorisation_id

    recovery_cases ||--o{ recovery_actions : case_id

    accounts ||--o{ ledger_entries : account_id
    ledger_transactions ||--o{ ledger_entries : transaction_id
    ledger_transactions |o--o| ledger_transactions : reverses_transaction_id
    concession_budgets ||--o{ budget_reservations : budget_id

    policy_bundles ||--o{ policy_rules : "bundle_id CASCADE"
    policy_bundles |o--o{ policy_bundles : supersedes_id

    domain_events {
        varchar id PK
        bigint sequence UK
    }
    audit_records {
        varchar id PK
        bigint sequence UK
    }
    outbox {
        varchar id PK
        varchar topic
    }
    processed_webhooks {
        varchar id PK
        varchar event_id UK
    }
    idempotency_records {
        varchar id PK
        varchar key UK
    }
    llm_calls {
        varchar id PK
        varchar kind
    }
    arm_assignments {
        varchar id PK
        varchar case_id UK
    }
    batch_results {
        varchar id PK
        varchar batch_id
    }
```

### Delete behaviour

Every foreign key in the schema is `ON DELETE RESTRICT` except two, which are `ON DELETE CASCADE`:

| Foreign key | Cascade rationale |
|---|---|
| `authorisation_usages.authorisation_id → authorisations.id` | Usage rows are meaningless without the authorisation they count against |
| `policy_rules.bundle_id → policy_bundles.id` | A rule has no identity outside its bundle |

`RESTRICT` everywhere else means a merchant, customer, subscription, authorisation, account or case
cannot be deleted while anything references it. DPDPA erasure therefore tombstones a customer — setting
`customers.erased_at` and replacing tokens — rather than deleting the row, so the ledger stays whole.

### References that are not foreign keys

Many `*_id` columns are plain `VARCHAR(32)` naming another table's primary key with no database-level
foreign key. They are documented here so the omission is not mistaken for an oversight in the reader's
model of the schema.

| Column | Names | Notes |
|---|---|---|
| `merchants.active_policy_bundle_id` | `policy_bundles.id` | Circular with `policy_bundles.merchant_id` |
| `recovery_cases.batch_id` | `recovery_batches.id` | |
| `recovery_actions.authorisation_id`, `.policy_bundle_id`, `.policy_rule_id`, `.approval_id`, `.reservation_id` | the corresponding tables | The self-justification trail on an action |
| `payment_attempts.case_id`, `.action_id`, `.subscription_id`, `.authorisation_id` | the corresponding tables | Only `merchant_id` is a real FK on this table |
| `ledger_transactions.case_id`, `.action_id`, `.customer_id` | the corresponding tables | Keeps the ledger writable independently of case lifecycle |
| `budget_reservations.case_id`, `.action_id`, `.customer_id` | the corresponding tables | |
| `policy_evaluations.case_id`, `.action_id`, `.bundle_id` | the corresponding tables | |
| `approvals.case_id`, `.action_id` | the corresponding tables | `action_id` is unique |
| `step_up_challenges.case_id`, `.action_id` | the corresponding tables | |
| `outreach_messages.case_id`, `.action_id`, `.consent_receipt_id` | the corresponding tables | |
| `contact_ledger.customer_id`, `.message_id` | `customers.id`, `outreach_messages.id` | Deliberate: the cap must survive message retention |
| `policy_bundles.compiled_from_call_id` | `llm_calls.id` | |
| `arm_assignments.batch_id`, `.case_id` | `recovery_batches.id`, `recovery_cases.id` | |
| `batch_results.batch_id` | `recovery_batches.id` | |
| `llm_calls.case_id`, `audit_records.case_id`/`.action_id`/`.merchant_id`, `domain_events.merchant_id`/`.aggregate_id` | the corresponding tables | Append-only tables hold no FKs at all |

`thread_id` is the other cross-table join: `recovery_cases.thread_id` is unique and identifies the
LangGraph thread for that case. `approvals.thread_id`, `step_up_challenges.thread_id` and
`audit_records.thread_id` carry the same value, which is what lets an audit row navigate to the
checkpoint that produced it.

---

## 5. Enumerated types

Enum columns are declared `sa.Enum(SomeEnum, native_enum=False, length=n)`. Three consequences follow,
all of them visible in the migration:

1. **No Postgres `ENUM` type is created.** The column is `VARCHAR(n)`. Adding a member is a code change
   only; it needs a migration only if the new name is longer than `n`.
2. **The stored value is the member *name*, upper case** — `INSUFFICIENT_FUNDS`, not the Python
   `StrEnum` value `insufficient_funds`. Application code deals in values; SQL written against the
   database must match names.
3. **No `CHECK` constraint is emitted.** SQLAlchemy's `Enum.create_constraint` defaults to false, and
   the initial migration contains no check constraint for any enum column. The closed vocabulary is
   enforced by the ORM and by the LLM layer constraining model output, not by the database.

`Currency` is the exception to rule 2: it goes through `CurrencyType`, not `sa.Enum`, and stores its
*value* (`INR`, `USD`) in a `VARCHAR(3)`.

| Enum | Column type | Columns | Members (as stored) |
|---|---|---|---|
| `FailureClass` | `VARCHAR(32)` | `recovery_cases.failure_class`, `payment_attempts.failure_class` | `INSUFFICIENT_FUNDS`, `INSTRUMENT_EXPIRED`, `ISSUER_TECHNICAL`, `LIMIT_EXCEEDED`, `MANDATE_REVOKED`, `MANDATE_PAUSED`, `ACCOUNT_CLOSED`, `RISK_DECLINED`, `AUTH_REQUIRED`, `UNKNOWN` |
| `AuthorisationType` | `VARCHAR(32)` | `authorisations.auth_type` | `UPI_AUTOPAY`, `ENACH`, `CARD_MANDATE`, `RESERVE_PAY`, `DELEGATED_AGENT` |
| `AuthorisationStatus` | `VARCHAR(32)` | `authorisations.status` | `ACTIVE`, `PAUSED`, `REVOKED`, `EXPIRED`, `EXHAUSTED` |
| `CaseStatus` | `VARCHAR(32)` | `recovery_cases.status` | `OPEN`, `DIAGNOSING`, `PLANNING`, `AWAITING_APPROVAL`, `AWAITING_STEP_UP`, `SCHEDULED`, `EXECUTING`, `PENDING_RECONCILIATION`, `RECOVERED`, `ABANDONED`, `UNRECOVERABLE`, `CHURNED` |
| `ActionType` | `VARCHAR(40)` | `recovery_actions.action_type` | `RETRY_DEBIT`, `SPLIT_DEBIT`, `REQUEST_INSTRUMENT_UPDATE`, `SEND_PAYMENT_LINK`, `REQUEST_MANDATE_REAUTH`, `TRIGGER_STEP_UP`, `SEND_REMINDER`, `SEND_DUNNING_NOTICE`, `GRANT_GRACE_PERIOD`, `OFFER_PARTIAL_PAYMENT`, `OFFER_PLAN_DOWNGRADE`, `OFFER_WINBACK_DISCOUNT`, `ESCALATE_TO_HUMAN`, `STOP_AND_WRITE_OFF`, `MARK_CHURNED` |
| `ActionStatus` | `VARCHAR(32)` | `recovery_actions.status` | `PROPOSED`, `DENIED_BY_POLICY`, `DENIED_BY_AUTHORISATION`, `AWAITING_APPROVAL`, `APPROVED`, `REJECTED`, `SCHEDULED`, `EXECUTING`, `SUCCEEDED`, `FAILED`, `UNKNOWN_OUTCOME`, `EXPIRED`, `CANCELLED` |
| `EntryDirection` | `VARCHAR(8)` | `ledger_entries.direction` | `DEBIT`, `CREDIT` |
| `AccountKind` | `VARCHAR(24)` | `accounts.kind` | `ASSET`, `LIABILITY`, `REVENUE`, `EXPENSE`, `CONTRA_REVENUE` |
| `LedgerTxnType` | `VARCHAR(40)` | `ledger_transactions.txn_type` | `RECEIVABLE_RECOGNISED`, `MANDATE_DEBIT_SETTLED`, `CONCESSION_GRANTED`, `CONCESSION_RESERVED`, `CONCESSION_RELEASED`, `BUDGET_FUNDED`, `CHANNEL_COST`, `MODEL_COST`, `WRITE_OFF`, `REVERSAL` |
| `PolicyEffect` | `VARCHAR(24)` | `policy_rules.effect`, `policy_evaluations.effect` | `ALLOW`, `DENY`, `REQUIRE_APPROVAL`, `CAP` |
| `PolicyBundleStatus` | `VARCHAR(24)` | `policy_bundles.status` | `DRAFT`, `PROPOSED`, `ACTIVE`, `SUPERSEDED`, `REJECTED` |
| `InterruptKind` | `VARCHAR(32)` | `approvals.kind`, `step_up_challenges.kind` | `HUMAN_APPROVAL`, `AFA_STEP_UP` |
| `ApprovalDecision` | `VARCHAR(16)` | `approvals.decision` | `APPROVE`, `REJECT`, `EDIT` |
| `Channel` | `VARCHAR(16)` | `outreach_messages.channel`, `contact_ledger.channel` | `EMAIL`, `SMS`, `WHATSAPP`, `PUSH`, `IN_APP`, `VOICE` |
| `MessagePurpose` | `VARCHAR(40)` | `consent_receipts.purpose`, `outreach_messages.purpose`, `contact_ledger.purpose` | `PAYMENT_FAILURE_NOTICE`, `PAYMENT_RECOVERY_OUTREACH`, `INSTRUMENT_UPDATE_REQUEST`, `MANDATE_REAUTHORISATION`, `STEP_UP_AUTHENTICATION`, `PROMOTIONAL_WINBACK` |
| `DeliveryStatus` | `VARCHAR(32)` | `outreach_messages.status` | `QUEUED`, `SUPPRESSED_NO_CONSENT`, `SUPPRESSED_FREQUENCY_CAP`, `SUPPRESSED_QUIET_HOURS`, `SENT`, `DELIVERED`, `READ`, `RESPONDED`, `BOUNCED`, `FAILED` |
| `ConsentState` | `VARCHAR(24)` | `consent_receipts.state` | `GRANTED`, `WITHDRAWN`, `EXPIRED`, `NEVER_GRANTED` |
| `ErasureStatus` | `VARCHAR(24)` | `erasure_requests.status` | `REQUESTED`, `IN_PROGRESS`, `COMPLETED`, `PARTIALLY_FAILED`, `DEAD_LETTERED` |
| `ExperimentArm` | `VARCHAR(16)` | `recovery_cases.experiment_arm`, `arm_assignments.arm`, `batch_results.arm` | `CONTROL`, `BASELINE`, `ANVIL` |
| `AuditEventType` | `VARCHAR(48)` | `audit_records.event_type` | `CASE_OPENED`, `FAILURE_CLASSIFIED`, `DIAGNOSIS_PRODUCED`, `PLAN_PRODUCED`, `AUTHORISATION_CHECKED`, `POLICY_EVALUATED`, `MODEL_SAFETY_EVENT`, `APPROVAL_REQUESTED`, `APPROVAL_RESOLVED`, `STEP_UP_REQUESTED`, `STEP_UP_RESOLVED`, `ACTION_EXECUTED`, `ACTION_OUTCOME`, `LEDGER_POSTED`, `MESSAGE_DISPATCHED`, `CONSENT_CHANGED`, `ERASURE_PROCESSED`, `CASE_CLOSED`, `WEBHOOK_RECEIVED`, `WEBHOOK_REJECTED` |
| `LLMCallKind` | `VARCHAR(24)` | `llm_calls.kind` | `CLASSIFY`, `DIAGNOSE`, `PLAN`, `COMPOSE`, `COMPILE_POLICY` |
| `Currency` | `VARCHAR(3)` | every `currency` column | `INR`, `USD` — stored as the *value* |

### Enums with no column of their own

`RunMode`, `RetryPosture`, `AuthorisationDecision` and `DenialReason` are defined in
`anvil/domain/enums.py` but are not bound to any enum column. Three columns hold their values as plain
strings rather than as enum columns, so the database does not know they are enumerated at all:

| Column | Type | Holds |
|---|---|---|
| `recovery_actions.authorisation_decision` | `VARCHAR(32)` | `AuthorisationDecision` |
| `recovery_actions.denial_reason` | `VARCHAR(48)` | `DenialReason` |
| `recovery_actions.policy_effect` | `VARCHAR(24)` | `PolicyEffect` |

Other free-text enumerations enforced only by a `CHECK` constraint or by convention:
`budget_reservations.state` (`held`, `settled`, `released`, checked), `audit_records.actor_kind`
(`agent`, `system`, `operator`, `customer`, checked), `authorisations.frequency` (`monthly`, `weekly`,
`as_presented`, unchecked), `subscriptions.status` (unchecked), `plans.interval` (unchecked),
`step_up_challenges.method` (unchecked).

---

## 6. Identifiers

Every primary key is a `VARCHAR(32)` holding a prefixed ULID: `<prefix>_<26 characters>` in Crockford
base32 (`0123456789ABCDEFGHJKMNPQRSTVWXYZ`, no I, L, O or U). The body encodes a 48-bit millisecond
timestamp followed by 80 bits of randomness, so ids sort lexicographically by creation time and
`ORDER BY id` is a valid chronological ordering with good index locality.

Ids are minted in the application by `anvil.core.ids.new_id(prefix)`, never by the database. No primary
key column has a default, a sequence or an identity: an `INSERT` without an id fails.

`deterministic_id(prefix, *parts)` derives a stable id from a blake2b-16 digest of its inputs, used
where a rerun with the same seed must produce a byte-identical database — the seeded simulator and
experiment assignment.

| Prefix | Table | Prefix | Table |
|---|---|---|---|
| `mch` | `merchants` | `pol` | `policy_bundles` |
| `cus` | `customers` | `prl` | `policy_rules` |
| `pln` | `plans` | `pev` | `policy_evaluations` |
| `sub` | `subscriptions` | `apr` | `approvals` |
| `aut` | `authorisations` | `cnt` | `consent_receipts` |
| `aus` | `authorisation_usages` | `msg` | `outreach_messages` |
| `stp` | `step_up_challenges` | `ers` | `erasure_requests` |
| `cse` | `recovery_cases` | `ctl` | `contact_ledger` |
| `act` | `recovery_actions` | `evt` | `domain_events` |
| `atm` | `payment_attempts` | `obx` | `outbox` |
| `acc` | `accounts` | `adt` | `audit_records` |
| `ltx` | `ledger_transactions` | `whk` | `processed_webhooks` |
| `len` | `ledger_entries` | `idm` | `idempotency_records` |
| `rsv` | `budget_reservations` | `llm` | `llm_calls` |
| `bgt` | `concession_budgets` | `bat` | `recovery_batches` |
| `asg` | `arm_assignments` | `bres` | `batch_results` |

`anvil.core.ids.IdPrefix` holds named constants for most of these. It additionally defines `mdt`
(mandate) and `exp` (experiment), which no table uses, and omits `obx`, `asg` and `bres`, which are
written as literals in the model definitions.

### Idempotency keys

`idempotency_key(*parts)` produces `anvil_<blake2b-16 hex>` — 38 characters in a `VARCHAR(96)` column.
The key depends only on the intent of an action, never on the attempt, so two retries of the same
logical debit produce the same key. Five columns carry one, four of them `NOT NULL`:

| Column | Nullable | Unique |
|---|---|---|
| `payment_attempts.idempotency_key` | no | yes |
| `ledger_transactions.idempotency_key` | no | yes |
| `budget_reservations.idempotency_key` | no | yes |
| `outreach_messages.idempotency_key` | no | yes |
| `recovery_actions.idempotency_key` | yes | yes |
| `idempotency_records.key` | no | yes |

`idempotency_records` covers Anvil's own inbound API surface; the other four protect outbound effects.

---

## 7. Time

Every timestamp column is `TIMESTAMP WITH TIME ZONE` through the `UTCDateTime` decorator, which refuses
a naive datetime at bind time and normalises everything to UTC in both directions. There is no column
anywhere that stores local time.

Three distinct sources of time appear in the schema:

| Kind | Set by | Examples |
|---|---|---|
| Row-lifecycle time | Postgres, `DEFAULT now()` | `created_at` on every table; `updated_at` on the 21 mutable tables |
| Business time | The application, from an injected `Clock` | `occurred_at`, `requested_at`, `effective_at`, `queued_at`, `contacted_at`, `original_failure_at`, `valid_from`, `period_start` |
| Scheduling time | The application | `next_action_at`, `scheduled_for`, `available_at`, `expires_at`, `next_attempt_at` |

`now()` in Postgres is transaction-start time, so every row written in one transaction shares a
`created_at`. That is deliberate: an event and the state change it describes commit together and are
timestamped together.

`updated_at` carries both a DDL `DEFAULT now()` and SQLAlchemy's `onupdate=now()`. The `onupdate` is
applied by SQLAlchemy to `UPDATE` statements it emits; it is not a database trigger, so a hand-written
`UPDATE` in psql will not bump it.

Business time never comes from `datetime.now()`. `anvil/core/clock.py` defines a `Clock` protocol with
`SystemClock` for production and `FrozenClock` for tests and the simulator, which is what lets a
thirty-day recovery horizon run in a second of wall time. `effective_at` on a ledger transaction exists
precisely so reporting time can differ from `created_at` when a settlement lands late.

IST (`UTC+05:30`) is defined in the same module because quiet hours, salary cycles and issuer
maintenance windows are IST concepts. They are computed in IST and stored in UTC.
`merchants.quiet_hours_start` and `quiet_hours_end` are the only place local hours are persisted, and
they are `SMALLINT` hour-of-day values, not timestamps.

---

## 8. Tables

### 8.1 Parties

#### `merchants`

A tenant. Owns its policy bundle, concession budget and cases.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `mch_`-prefixed ULID |
| `name` | `VARCHAR(200)` | | — | Display name |
| `legal_name` | `VARCHAR(300)` | yes | — | Registered entity name |
| `razorpay_account_id` | `VARCHAR(64)` | yes | — | Razorpay account handle |
| `currency` | `VARCHAR(3)` | | `INR` | Settlement currency |
| `quiet_hours_start` | `SMALLINT` | | `21` | First IST hour during which no outreach may be sent |
| `quiet_hours_end` | `SMALLINT` | | `8` | IST hour at which outreach may resume |
| `review_first` | `BOOLEAN` | | `true` | When true, every action queues for a human instead of acting autonomously inside policy |
| `active_policy_bundle_id` | `VARCHAR(32)` | yes | — | The currently active bundle; not a foreign key |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `updated_at` | `TIMESTAMPTZ` | | `now()` | Last ORM update |

Primary key `pk_merchants (id)`. Unique `uq_merchants_razorpay_account_id (razorpay_account_id)`.
Checks `ck_merchants_quiet_start_range` (`quiet_hours_start BETWEEN 0 AND 23`) and
`ck_merchants_quiet_end_range` (`quiet_hours_end BETWEEN 0 AND 23`). Index `ix_merchants_created_at`.

#### `customers`

A data principal under the DPDPA. Contact details are tokenised: an irreversible pseudonym plus a
display-safe hint, never a raw phone number, email address or VPA.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `cus_`-prefixed ULID |
| `external_ref` | `VARCHAR(64)` | yes | — | The merchant's own identifier for this customer |
| `email_token` | `VARCHAR(64)` | yes | — | Irreversible pseudonym for the email address |
| `email_hint` | `VARCHAR(64)` | yes | — | Display-safe fragment |
| `phone_token` | `VARCHAR(64)` | yes | — | Irreversible pseudonym for the phone number |
| `phone_hint` | `VARCHAR(32)` | yes | — | Display-safe fragment |
| `vpa_token` | `VARCHAR(64)` | yes | — | Irreversible pseudonym for the UPI VPA |
| `vpa_hint` | `VARCHAR(64)` | yes | — | Display-safe fragment |
| `display_name` | `VARCHAR(120)` | yes | — | Name shown to an operator |
| `preferred_language` | `VARCHAR(8)` | | `en` | Language for outreach |
| `timezone` | `VARCHAR(48)` | | `Asia/Kolkata` | IANA zone for local-time decisions |
| `tenure_days` | `INTEGER` | | `0` | Days since the relationship began |
| `lifetime_value_minor` | `BIGINT` | | — | Denormalised lifetime value in minor units |
| `prior_failures` | `INTEGER` | | `0` | Historic debit failures |
| `prior_recoveries` | `INTEGER` | | `0` | Historic successful recoveries |
| `prior_concessions_minor` | `BIGINT` | | — | Total conceded historically, minor units |
| `erased_at` | `TIMESTAMPTZ` | yes | — | Set when a DPDPA erasure completed; the row is tombstoned, not deleted |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `updated_at` | `TIMESTAMPTZ` | | `now()` | Last ORM update |
| `merchant_id` | `VARCHAR(32)` | | — | Owning tenant |

Primary key `pk_customers (id)`. Foreign key `fk_customers_merchant_id_merchants → merchants.id`
`RESTRICT`. Check `ck_customers_ltv_non_negative` (`lifetime_value_minor >= 0`). Unique index
`ix_customers_merchant_external (merchant_id, external_ref)`. Indexes on `created_at`, `email_token`,
`external_ref`, `merchant_id`, `phone_token`, `vpa_token`.

The behavioural counters are recomputed by the risk module. They feed scoring and the planner's
context; nothing in the ledger reads them.

#### `plans`

A priced subscription tier. Downgrades target a cheaper plan in the same `family`.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `pln_`-prefixed ULID |
| `name` | `VARCHAR(120)` | | — | Display name |
| `family` | `VARCHAR(64)` | | — | Grouping within which downgrades are permitted |
| `tier_rank` | `INTEGER` | | `0` | Ordering within the family |
| `amount_minor` | `BIGINT` | | — | Price in minor units |
| `currency` | `VARCHAR(3)` | | `INR` | Price currency |
| `interval` | `VARCHAR(16)` | | `monthly` | Billing interval |
| `razorpay_plan_id` | `VARCHAR(64)` | yes | — | Razorpay plan handle |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `updated_at` | `TIMESTAMPTZ` | | `now()` | Last ORM update |
| `merchant_id` | `VARCHAR(32)` | | — | Owning tenant |

Primary key `pk_plans (id)`. Foreign key `fk_plans_merchant_id_merchants → merchants.id` `RESTRICT`.
Unique `uq_plans_razorpay_plan_id (razorpay_plan_id)`. Check `ck_plans_plan_amount_positive`
(`amount_minor > 0`). Indexes on `created_at`, `family`, `merchant_id`.

#### `subscriptions`

A recurring commitment, backed by exactly one active authorisation.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `sub_`-prefixed ULID |
| `customer_id` | `VARCHAR(32)` | | — | Subscriber |
| `plan_id` | `VARCHAR(32)` | | — | Plan subscribed to |
| `razorpay_subscription_id` | `VARCHAR(64)` | yes | — | Razorpay subscription handle |
| `status` | `VARCHAR(24)` | | `active` | Lifecycle state, free text |
| `started_at` | `TIMESTAMPTZ` | | — | When the subscription began |
| `current_period_start` | `TIMESTAMPTZ` | | — | Start of the current billing period |
| `current_period_end` | `TIMESTAMPTZ` | | — | End of the current billing period |
| `cancelled_at` | `TIMESTAMPTZ` | yes | — | When cancellation took effect |
| `amount_minor` | `BIGINT` | | — | Price cached from the plan, so a mid-cycle price change does not rewrite history |
| `currency` | `VARCHAR(3)` | | `INR` | Price currency |
| `consecutive_failures` | `INTEGER` | | `0` | Unbroken run of failed debits |
| `notes` | `TEXT` | yes | — | Operator notes |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `updated_at` | `TIMESTAMPTZ` | | `now()` | Last ORM update |
| `merchant_id` | `VARCHAR(32)` | | — | Owning tenant |

Primary key `pk_subscriptions (id)`. Foreign keys to `customers.id`, `plans.id` and `merchants.id`, all
`RESTRICT`. Unique `uq_subscriptions_razorpay_subscription_id`. Checks
`ck_subscriptions_subscription_amount_positive` (`amount_minor > 0`) and
`ck_subscriptions_failures_non_negative` (`consecutive_failures >= 0`). Composite index
`ix_subscriptions_merchant_status (merchant_id, status)`; single-column indexes on `created_at`,
`current_period_end`, `customer_id`, `merchant_id`, `status`.

### 8.2 Authorisation

#### `authorisations`

A stored right to debit, of one of five kinds. The kinds share one table because the check is uniform —
amount, window, frequency, counterparty, remaining capacity — so there is exactly one code path that
can say "authorised".

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `aut_`-prefixed ULID |
| `customer_id` | `VARCHAR(32)` | | — | Principal who granted it |
| `subscription_id` | `VARCHAR(32)` | yes | — | Subscription it backs, when tied to one |
| `auth_type` | `VARCHAR(32)` | | — | `AuthorisationType` |
| `status` | `VARCHAR(32)` | | `ACTIVE` | `AuthorisationStatus` |
| `external_reference` | `VARCHAR(96)` | yes | — | UMN for UPI Autopay, UMRN for e-NACH, token ref for cards |
| `max_amount_minor` | `BIGINT` | | — | Ceiling for a single debit; always set |
| `period_cap_minor` | `BIGINT` | yes | — | Ceiling across `period_days`; null means unlimited within the window |
| `period_days` | `SMALLINT` | yes | — | Length of the capped window |
| `currency` | `VARCHAR(3)` | | `INR` | Currency of every limit on the row |
| `frequency` | `VARCHAR(24)` | | `monthly` | `monthly`, `weekly` or `as_presented`; governs frequency violations |
| `max_attempts_per_cycle` | `SMALLINT` | | `3` | Debit attempts permitted per billing cycle |
| `valid_from` | `TIMESTAMPTZ` | | — | Start of the validity window |
| `valid_until` | `TIMESTAMPTZ` | yes | — | End of the validity window |
| `blocked_amount_minor` | `BIGINT` | yes | — | Reserve Pay: amount blocked up front |
| `consumed_amount_minor` | `BIGINT` | | — | Reserve Pay: amount drawn down so far |
| `delegated_to_agent` | `VARCHAR(64)` | yes | — | Named agent, when the principal delegated rather than authorising the merchant directly |
| `agent_per_txn_cap_minor` | `BIGINT` | yes | — | Delegated per-transaction cap |
| `agent_period_cap_minor` | `BIGINT` | yes | — | Delegated per-period cap |
| `revoked_at` | `TIMESTAMPTZ` | yes | — | When the principal revoked it |
| `revocation_reason` | `TEXT` | yes | — | Why |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `updated_at` | `TIMESTAMPTZ` | | `now()` | Last ORM update |
| `merchant_id` | `VARCHAR(32)` | | — | Owning tenant |

Primary key `pk_authorisations (id)`. Foreign keys to `customers.id`, `subscriptions.id` and
`merchants.id`, all `RESTRICT`. Checks:

| Constraint | Expression |
|---|---|
| `ck_authorisations_max_amount_positive` | `max_amount_minor > 0` |
| `ck_authorisations_consumed_non_negative` | `consumed_amount_minor >= 0` |
| `ck_authorisations_block_not_overdrawn` | `blocked_amount_minor IS NULL OR consumed_amount_minor <= blocked_amount_minor` |
| `ck_authorisations_period_cap_needs_days` | `period_cap_minor IS NULL OR period_days IS NOT NULL` |
| `ck_authorisations_attempts_positive` | `max_attempts_per_cycle > 0` |

Composite indexes `ix_authorisations_customer_status (customer_id, status)` and
`ix_authorisations_sub_active (subscription_id, status)`; single-column indexes on `auth_type`,
`created_at`, `customer_id`, `delegated_to_agent`, `external_reference`, `merchant_id`, `status`,
`subscription_id`, `valid_until`.

A delegated agent's caps are checked in addition to the principal's, and the tighter of the two wins.
`ck_authorisations_block_not_overdrawn` is what makes a Reserve Pay block structurally incapable of
going negative.

#### `authorisation_usages`

Consumption of an authorisation within one billing cycle. A cycle rollover is a new row, not a
destructive reset, so the history of how much of a mandate was used and when survives.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `aus_`-prefixed ULID |
| `authorisation_id` | `VARCHAR(32)` | | — | Authorisation being consumed |
| `cycle_start` | `TIMESTAMPTZ` | | — | Start of the billing cycle |
| `cycle_end` | `TIMESTAMPTZ` | | — | End of the billing cycle |
| `attempts_used` | `SMALLINT` | | `0` | Debit attempts spent in this cycle |
| `amount_debited_minor` | `BIGINT` | | — | Total debited in this cycle, minor units |
| `last_attempt_at` | `TIMESTAMPTZ` | yes | — | Most recent attempt |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `updated_at` | `TIMESTAMPTZ` | | `now()` | Last ORM update |

Primary key `pk_authorisation_usages (id)`. Foreign key
`fk_authorisation_usages_authorisation_id_authorisations → authorisations.id` **`CASCADE`**. Unique
`uq_usage_auth_cycle (authorisation_id, cycle_start)`. Checks
`ck_authorisation_usages_attempts_used_non_negative` (`attempts_used >= 0`) and
`ck_authorisation_usages_debited_non_negative` (`amount_debited_minor >= 0`). Indexes on
`authorisation_id` and `created_at`.

This table is not merchant-scoped; tenancy is reached through the authorisation.

#### `step_up_challenges`

An Additional Factor of Authentication journey the graph is waiting on. Created when an action is
within the principal's authority but outside the agent's delegated cap, or when the issuer demands AFA.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `stp_`-prefixed ULID |
| `case_id` | `VARCHAR(32)` | | — | Case being blocked |
| `action_id` | `VARCHAR(32)` | yes | — | Action being blocked |
| `authorisation_id` | `VARCHAR(32)` | | — | Authorisation the step-up is against |
| `customer_id` | `VARCHAR(32)` | | — | Customer being challenged |
| `kind` | `VARCHAR(32)` | | `AFA_STEP_UP` | `InterruptKind` |
| `method` | `VARCHAR(24)` | | `otp` | Challenge method |
| `requested_amount_minor` | `BIGINT` | | — | Amount the step-up would authorise |
| `currency` | `VARCHAR(3)` | | `INR` | Amount currency |
| `challenge_digest` | `VARCHAR(64)` | | — | Salted digest, never the OTP itself, so a database dump cannot be replayed to approve a debit |
| `attempts` | `SMALLINT` | | `0` | Answers submitted |
| `max_attempts` | `SMALLINT` | | `3` | Answers permitted |
| `expires_at` | `TIMESTAMPTZ` | | — | When the challenge lapses |
| `resolved_at` | `TIMESTAMPTZ` | yes | — | When it was answered or abandoned |
| `succeeded` | `BOOLEAN` | yes | — | Outcome; null while pending |
| `thread_id` | `VARCHAR(64)` | | — | LangGraph thread this challenge blocks, so resolution can resume it |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `updated_at` | `TIMESTAMPTZ` | | `now()` | Last ORM update |
| `merchant_id` | `VARCHAR(32)` | | — | Owning tenant |

Primary key `pk_step_up_challenges (id)`. Foreign keys to `authorisations.id`, `customers.id` and
`merchants.id`, all `RESTRICT`; `case_id` and `action_id` are not foreign keys. Checks
`ck_step_up_challenges_stepup_attempts` (`attempts >= 0 AND attempts <= max_attempts`) and
`ck_step_up_challenges_stepup_amount_positive` (`requested_amount_minor > 0`). Composite index
`ix_stepup_pending (merchant_id, resolved_at)`; single-column indexes on `action_id`, `case_id`,
`created_at`, `customer_id`, `expires_at`, `merchant_id`, `thread_id`.

### 8.3 Recovery

#### `recovery_cases`

One at-risk invoice, worked from failure to a terminal outcome. Exactly one LangGraph thread per case;
`thread_id` is the join between the durable graph state and this relational read model.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `cse_`-prefixed ULID |
| `customer_id` | `VARCHAR(32)` | | — | Customer at risk |
| `subscription_id` | `VARCHAR(32)` | | — | Subscription whose debit failed |
| `authorisation_id` | `VARCHAR(32)` | yes | — | Authorisation available to work the case |
| `batch_id` | `VARCHAR(32)` | yes | — | Experiment batch, when part of one |
| `status` | `VARCHAR(32)` | | `OPEN` | `CaseStatus` |
| `thread_id` | `VARCHAR(64)` | | — | LangGraph thread id; unique |
| `amount_at_risk_minor` | `BIGINT` | | — | Money at stake, minor units |
| `amount_recovered_minor` | `BIGINT` | | — | Money recovered so far |
| `concession_granted_minor` | `BIGINT` | | — | Money given up to recover it |
| `currency` | `VARCHAR(3)` | | `INR` | Currency of the three amounts |
| `original_failure_at` | `TIMESTAMPTZ` | | — | When the debit first failed |
| `raw_failure_code` | `VARCHAR(64)` | yes | — | Issuer or NPCI code as received |
| `raw_failure_description` | `TEXT` | yes | — | Issuer text as received |
| `failure_class` | `VARCHAR(32)` | yes | — | `FailureClass` |
| `classified_deterministically` | `BOOLEAN` | yes | — | True when the deterministic table resolved it, false when the LLM did |
| `recovery_likelihood` | `SMALLINT` | yes | — | Integer score, 0–1000 |
| `churn_risk` | `SMALLINT` | yes | — | Integer score, 0–1000 |
| `priority_score` | `INTEGER` | yes | — | Work-queue ordering |
| `experiment_arm` | `VARCHAR(16)` | yes | — | `ExperimentArm` |
| `attempts_made` | `SMALLINT` | | `0` | Debit attempts spent on this case |
| `contacts_made` | `SMALLINT` | | `0` | Outreach messages sent |
| `next_action_at` | `TIMESTAMPTZ` | yes | — | When the case is next due |
| `closed_at` | `TIMESTAMPTZ` | yes | — | When it reached a terminal status |
| `closure_reason` | `TEXT` | yes | — | Why it closed |
| `diagnosis` | `JSONB` | yes | — | The model's working diagnosis; advisory, and never trusted without a policy pass |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `updated_at` | `TIMESTAMPTZ` | | `now()` | Last ORM update |
| `merchant_id` | `VARCHAR(32)` | | — | Owning tenant |

Primary key `pk_recovery_cases (id)`. Foreign keys to `customers.id`, `subscriptions.id`,
`authorisations.id` and `merchants.id`, all `RESTRICT`. Unique `uq_recovery_cases_thread_id`. Checks:

| Constraint | Expression |
|---|---|
| `ck_recovery_cases_case_at_risk_positive` | `amount_at_risk_minor > 0` |
| `ck_recovery_cases_case_recovered_non_negative` | `amount_recovered_minor >= 0` |
| `ck_recovery_cases_case_concession_non_negative` | `concession_granted_minor >= 0` |
| `ck_recovery_cases_case_likelihood_range` | `recovery_likelihood IS NULL OR recovery_likelihood BETWEEN 0 AND 1000` |
| `ck_recovery_cases_case_churn_range` | `churn_risk IS NULL OR churn_risk BETWEEN 0 AND 1000` |
| `ck_recovery_cases_case_attempts_non_negative` | `attempts_made >= 0` |
| `ck_recovery_cases_case_contacts_non_negative` | `contacts_made >= 0` |

Composite indexes `ix_cases_merchant_status_priority (merchant_id, status, priority_score)`,
`ix_cases_due (status, next_action_at)` and `ix_cases_batch_arm (batch_id, experiment_arm)`;
single-column indexes on `authorisation_id`, `batch_id`, `created_at`, `customer_id`, `experiment_arm`,
`failure_class`, `merchant_id`, `next_action_at`, `original_failure_at`, `priority_score`, `status`,
`subscription_id`.

Scores are integers on a 0–1000 scale so they sort and compare exactly. `RECOVERED`, `ABANDONED`,
`UNRECOVERABLE` and `CHURNED` are the terminal statuses.

#### `recovery_actions`

One proposed step, from proposal through authorisation, policy and outcome. Every action carries the
evidence for its own legitimacy: which authorisation permitted it, which policy rule allowed it, who
approved it. Reading a row tells you why it was allowed to happen without joining to anything.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `act_`-prefixed ULID |
| `case_id` | `VARCHAR(32)` | | — | Case this belongs to |
| `sequence` | `SMALLINT` | | `0` | Position within the case |
| `action_type` | `VARCHAR(40)` | | — | `ActionType` |
| `status` | `VARCHAR(32)` | | `PROPOSED` | `ActionStatus` |
| `amount_minor` | `BIGINT` | yes | — | Money the action would move or concede |
| `currency` | `VARCHAR(3)` | | `INR` | Amount currency |
| `payload` | `JSONB` | | `{}` | Action parameters |
| `rationale` | `TEXT` | yes | — | Why the planner proposed this; shown verbatim to the approving operator |
| `model_confidence` | `SMALLINT` | yes | — | Model's confidence, 0–100 |
| `authorisation_id` | `VARCHAR(32)` | yes | — | Authorisation presented |
| `authorisation_decision` | `VARCHAR(32)` | yes | — | `AuthorisationDecision`, as a plain string |
| `denial_reason` | `VARCHAR(48)` | yes | — | `DenialReason`, as a plain string |
| `policy_bundle_id` | `VARCHAR(32)` | yes | — | Bundle evaluated against |
| `policy_rule_id` | `VARCHAR(32)` | yes | — | Rule that matched |
| `policy_effect` | `VARCHAR(24)` | yes | — | `PolicyEffect`, as a plain string |
| `approval_id` | `VARCHAR(32)` | yes | — | Human approval, when one was required |
| `reservation_id` | `VARCHAR(32)` | yes | — | Budget hold, when a concession was involved |
| `idempotency_key` | `VARCHAR(96)` | yes | — | Stable across retries of this logical action |
| `scheduled_for` | `TIMESTAMPTZ` | yes | — | When execution is due |
| `executed_at` | `TIMESTAMPTZ` | yes | — | When execution started |
| `outcome_at` | `TIMESTAMPTZ` | yes | — | When the outcome was known |
| `outcome_detail` | `JSONB` | yes | — | Structured outcome |
| `expected_recovery_minor` | `BIGINT` | yes | — | Expected value at proposal time, for scheduler calibration |
| `expected_probability_bps` | `SMALLINT` | yes | — | Expected settle probability in basis points |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `updated_at` | `TIMESTAMPTZ` | | `now()` | Last ORM update |
| `merchant_id` | `VARCHAR(32)` | | — | Owning tenant |

Primary key `pk_recovery_actions (id)`. Foreign keys to `recovery_cases.id` and `merchants.id`, both
`RESTRICT`. Uniques `uq_action_case_sequence (case_id, sequence)` and
`uq_recovery_actions_idempotency_key (idempotency_key)`. Checks:

| Constraint | Expression |
|---|---|
| `ck_recovery_actions_action_amount_positive` | `amount_minor IS NULL OR amount_minor > 0` |
| `ck_recovery_actions_action_confidence_range` | `model_confidence IS NULL OR model_confidence BETWEEN 0 AND 100` |
| `ck_recovery_actions_action_probability_range` | `expected_probability_bps IS NULL OR expected_probability_bps BETWEEN 0 AND 10000` |

Composite indexes `ix_actions_due (status, scheduled_for)` and
`ix_actions_merchant_status (merchant_id, status)`; single-column indexes on `action_type`,
`approval_id`, `authorisation_id`, `case_id`, `created_at`, `merchant_id`, `reservation_id`,
`scheduled_for`, `status`.

#### `payment_attempts`

A concrete debit attempt against the gateway. Separate from `recovery_actions` because one action can
produce several attempts — a split debit is one action and three attempts — and because an attempt has
its own gateway identity and its own unknown-outcome path.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `atm_`-prefixed ULID |
| `case_id` | `VARCHAR(32)` | | — | Case this attempt serves |
| `action_id` | `VARCHAR(32)` | yes | — | Action that produced it |
| `subscription_id` | `VARCHAR(32)` | | — | Subscription being debited |
| `authorisation_id` | `VARCHAR(32)` | yes | — | Authorisation presented |
| `attempt_number` | `SMALLINT` | | `1` | Ordinal within the case |
| `amount_minor` | `BIGINT` | | — | Amount attempted, minor units |
| `currency` | `VARCHAR(3)` | | `INR` | Amount currency |
| `idempotency_key` | `VARCHAR(96)` | | — | Key sent to the gateway; unique |
| `razorpay_payment_id` | `VARCHAR(64)` | yes | — | Gateway payment handle; unique |
| `razorpay_order_id` | `VARCHAR(64)` | yes | — | Gateway order handle |
| `requested_at` | `TIMESTAMPTZ` | | — | When the call was made |
| `settled_at` | `TIMESTAMPTZ` | yes | — | When money settled |
| `succeeded` | `BOOLEAN` | yes | — | Outcome; null while unknown |
| `raw_failure_code` | `VARCHAR(64)` | yes | — | Issuer or NPCI code as received |
| `raw_failure_description` | `TEXT` | yes | — | Issuer text as received |
| `failure_class` | `VARCHAR(32)` | yes | — | `FailureClass` |
| `needs_reconciliation` | `BOOLEAN` | | `false` | True while the gateway outcome is genuinely unknown |
| `reconciled_at` | `TIMESTAMPTZ` | yes | — | When the reconciler resolved it |
| `predicted_probability_bps` | `SMALLINT` | yes | — | What the scheduler predicted, so calibration can be measured |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `updated_at` | `TIMESTAMPTZ` | | `now()` | Last ORM update |
| `merchant_id` | `VARCHAR(32)` | | — | Owning tenant |

Primary key `pk_payment_attempts (id)`. The only foreign key is
`fk_payment_attempts_merchant_id_merchants → merchants.id` `RESTRICT`; `case_id`, `action_id`,
`subscription_id` and `authorisation_id` are plain columns. Uniques
`uq_payment_attempts_idempotency_key` and `uq_payment_attempts_razorpay_payment_id`. Checks
`ck_payment_attempts_attempt_amount_positive` (`amount_minor > 0`) and
`ck_payment_attempts_attempt_number_positive` (`attempt_number > 0`). Composite indexes
`ix_attempts_case_number (case_id, attempt_number)` and
`ix_attempts_unreconciled (needs_reconciliation, requested_at)`; single-column indexes on `action_id`,
`authorisation_id`, `case_id`, `created_at`, `failure_class`, `merchant_id`, `needs_reconciliation`,
`raw_failure_code`, `razorpay_order_id`, `subscription_id`, `succeeded`.

`needs_reconciliation` is the unknown-outcome path: a reconciler polls with the same idempotency key
rather than blindly retrying.

### 8.4 Ledger

#### `accounts`

A node in the chart of accounts. `code` is the stable handle used in code
(`merchant:receivable`, `merchant:concession_budget`); ids are for foreign keys.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `acc_`-prefixed ULID |
| `code` | `VARCHAR(64)` | | — | Stable handle |
| `name` | `VARCHAR(160)` | | — | Human-readable name |
| `kind` | `VARCHAR(24)` | | — | `AccountKind`; determines normal balance |
| `currency` | `VARCHAR(3)` | | `INR` | Account currency |
| `customer_id` | `VARCHAR(32)` | yes | — | Set for per-customer sub-accounts; null for merchant-level accounts |
| `description` | `TEXT` | yes | — | Free text |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `updated_at` | `TIMESTAMPTZ` | | `now()` | Last ORM update |
| `merchant_id` | `VARCHAR(32)` | | — | Owning tenant |

Primary key `pk_accounts (id)`. Foreign keys to `customers.id` and `merchants.id`, both `RESTRICT`.
Unique `uq_account_scope (merchant_id, code, customer_id)`. Composite index
`ix_accounts_merchant_kind (merchant_id, kind)`; single-column indexes on `created_at`, `customer_id`,
`merchant_id`.

Assets, expenses and contra-revenue accounts increase on the debit side; liabilities and revenue on the
credit side. No balance is stored on this table.

#### `ledger_transactions`

A balanced set of entries, committed atomically. **Append-only, enforced by trigger.** A mistake is
corrected by posting a reversal that references this row through `reverses_transaction_id`, never by
editing it.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `ltx_`-prefixed ULID |
| `txn_type` | `VARCHAR(40)` | | — | `LedgerTxnType` |
| `currency` | `VARCHAR(3)` | | `INR` | Transaction currency |
| `effective_at` | `TIMESTAMPTZ` | | — | Effective date for reporting, which may differ from `created_at` when a settlement lands late |
| `case_id` | `VARCHAR(32)` | yes | — | Case this posting relates to |
| `action_id` | `VARCHAR(32)` | yes | — | Action this posting relates to |
| `customer_id` | `VARCHAR(32)` | yes | — | Customer this posting relates to |
| `idempotency_key` | `VARCHAR(96)` | | — | Stable across retries of the same logical posting; unique |
| `reverses_transaction_id` | `VARCHAR(32)` | yes | — | The transaction this one reverses; unique, so a transaction can be reversed at most once |
| `narration` | `TEXT` | | — | Human-readable description |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `merchant_id` | `VARCHAR(32)` | | — | Owning tenant |

Primary key `pk_ledger_transactions (id)`. Foreign keys
`fk_ledger_transactions_merchant_id_merchants → merchants.id` and
`fk_ledger_transactions_reverses_transaction_id_ledger_transactions → ledger_transactions.id`, both
`RESTRICT`. Uniques `uq_ledger_transactions_idempotency_key` and
`uq_ledger_transactions_reverses_transaction_id`. Composite indexes
`ix_ledger_txn_merchant_effective (merchant_id, effective_at)` and
`ix_ledger_txn_case (case_id, created_at)`; single-column indexes on `action_id`, `case_id`,
`created_at`, `customer_id`, `effective_at`, `merchant_id`, `txn_type`.

No `updated_at`: the table has no update path.

#### `ledger_entries`

One side of one posting. **Append-only, enforced by trigger.** `amount_minor` is always strictly
positive and direction is carried explicitly rather than by sign, so a sign error cannot quietly turn a
debit into a credit.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `len_`-prefixed ULID |
| `transaction_id` | `VARCHAR(32)` | | — | Transaction this leg belongs to |
| `account_id` | `VARCHAR(32)` | | — | Account debited or credited |
| `direction` | `VARCHAR(8)` | | — | `EntryDirection`: `DEBIT` or `CREDIT` |
| `amount_minor` | `BIGINT` | | — | Strictly positive amount in minor units |
| `currency` | `VARCHAR(3)` | | `INR` | Entry currency |
| `sequence` | `SMALLINT` | | `0` | Position within the transaction, so replay is deterministically ordered |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |

Primary key `pk_ledger_entries (id)`. Foreign keys to `ledger_transactions.id` and `accounts.id`, both
`RESTRICT`. Unique `uq_entry_txn_sequence (transaction_id, sequence)`. Check
`ck_ledger_entries_entry_amount_strictly_positive` (`amount_minor > 0`). Composite index
`ix_ledger_entries_account_created (account_id, created_at)`; single-column indexes on `account_id`,
`created_at`, `transaction_id`.

Balances are never stored. They are derived by summing entries, which means a balance cannot drift from
its history because there is nothing to drift. That "sum of debits equals sum of credits per
transaction" is asserted by the posting service inside the same transaction that writes the entries;
the database does not check it, because a per-transaction sum is not expressible as a row constraint.

#### `concession_budgets`

The merchant-authorised pot the agent may concede from. Deliberately stores no computed balance:
available headroom is `funded − settled − held`, all derived. This row is the lock target for
concurrent reservations.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `bgt_`-prefixed ULID |
| `period_start` | `TIMESTAMPTZ` | | — | Start of the budget period |
| `period_end` | `TIMESTAMPTZ` | | — | End of the budget period |
| `funded_minor` | `BIGINT` | | — | Ceiling for the period, minor units |
| `currency` | `VARCHAR(3)` | | `INR` | Budget currency |
| `per_customer_cap_minor` | `BIGINT` | | — | Hard ceiling per customer, independent of headroom |
| `per_action_cap_minor` | `BIGINT` | | — | Hard ceiling per action |
| `max_percent_of_mrr` | `SMALLINT` | | `25` | Maximum concession as a percentage of the subscription's monthly value |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `updated_at` | `TIMESTAMPTZ` | | `now()` | Last ORM update |
| `merchant_id` | `VARCHAR(32)` | | — | Owning tenant |

Primary key `pk_concession_budgets (id)`. Foreign key to `merchants.id` `RESTRICT`. Unique
`uq_budget_merchant_period (merchant_id, period_start)`. Checks:

| Constraint | Expression |
|---|---|
| `ck_concession_budgets_budget_funded_non_negative` | `funded_minor >= 0` |
| `ck_concession_budgets_budget_customer_cap_positive` | `per_customer_cap_minor > 0` |
| `ck_concession_budgets_budget_action_cap_positive` | `per_action_cap_minor > 0` |
| `ck_concession_budgets_budget_mrr_percent_range` | `max_percent_of_mrr BETWEEN 0 AND 100` |
| `ck_concession_budgets_budget_period_ordered` | `period_end > period_start` |

Indexes on `created_at`, `merchant_id`, `period_end`.

#### `budget_reservations`

A hold against a merchant's concession budget. A concession is reserved under `SELECT … FOR UPDATE` on
the budget row *before* the action executes, and settled or released afterwards, so two cases running
concurrently cannot jointly overspend a budget with room for only one.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `rsv_`-prefixed ULID |
| `budget_id` | `VARCHAR(32)` | | — | Budget held against |
| `case_id` | `VARCHAR(32)` | | — | Case the hold serves |
| `action_id` | `VARCHAR(32)` | yes | — | Action the hold serves |
| `customer_id` | `VARCHAR(32)` | | — | Customer the concession is for |
| `amount_minor` | `BIGINT` | | — | Amount held, minor units |
| `currency` | `VARCHAR(3)` | | `INR` | Hold currency |
| `state` | `VARCHAR(16)` | | `held` | `held` → `settled` when the concession lands, or `released` when the action is rejected, expires or fails |
| `expires_at` | `TIMESTAMPTZ` | | — | When the hold lapses |
| `settled_at` | `TIMESTAMPTZ` | yes | — | When it settled |
| `released_at` | `TIMESTAMPTZ` | yes | — | When it was released |
| `idempotency_key` | `VARCHAR(96)` | | — | Stable across retries, so a replayed action reuses its hold; unique |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `updated_at` | `TIMESTAMPTZ` | | `now()` | Last ORM update |
| `merchant_id` | `VARCHAR(32)` | | — | Owning tenant |

Primary key `pk_budget_reservations (id)`. Foreign keys to `concession_budgets.id` and `merchants.id`,
both `RESTRICT`. Unique `uq_budget_reservations_idempotency_key`. Checks
`ck_budget_reservations_reservation_amount_positive` (`amount_minor > 0`) and
`ck_budget_reservations_reservation_state_valid` (`state IN ('held','settled','released')`). Composite
index `ix_reservations_budget_state (budget_id, state)`; single-column indexes on `action_id`,
`budget_id`, `case_id`, `created_at`, `customer_id`, `expires_at`, `merchant_id`, `state`.

### 8.5 Policy

#### `policy_bundles`

An immutable, content-addressed set of rules. A bundle is never edited: compiling merchant prose
produces a *new* bundle in `PROPOSED`, a human reviews the diff and activates it, and the previous one
is superseded. `content_hash` makes "is this the bundle that was approved?" a byte comparison rather
than a judgement.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `pol_`-prefixed ULID |
| `version` | `INTEGER` | | — | Monotonic version within the merchant |
| `status` | `VARCHAR(24)` | | `DRAFT` | `PolicyBundleStatus` |
| `content_hash` | `VARCHAR(64)` | | — | Digest of the rule set |
| `source_text` | `TEXT` | yes | — | The prose the merchant wrote, when compiled |
| `compiled_by_model` | `VARCHAR(64)` | yes | — | Model that produced it |
| `compiled_from_call_id` | `VARCHAR(32)` | yes | — | `llm_calls.id` of the compilation |
| `diff_summary` | `TEXT` | yes | — | Plain-language summary of what changed versus the superseded bundle |
| `supersedes_id` | `VARCHAR(32)` | yes | — | Bundle this replaces |
| `activated_at` | `TIMESTAMPTZ` | yes | — | When it became active |
| `activated_by` | `VARCHAR(120)` | yes | — | Who activated it |
| `rejected_reason` | `TEXT` | yes | — | Why it was rejected |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `updated_at` | `TIMESTAMPTZ` | | `now()` | Last ORM update |
| `merchant_id` | `VARCHAR(32)` | | — | Owning tenant |

Primary key `pk_policy_bundles (id)`. Foreign keys
`fk_policy_bundles_supersedes_id_policy_bundles → policy_bundles.id` and to `merchants.id`, both
`RESTRICT`. Unique `uq_bundle_merchant_version (merchant_id, version)`. Check
`ck_policy_bundles_bundle_version_positive` (`version > 0`). Composite index
`ix_bundles_merchant_status (merchant_id, status)`; single-column indexes on `content_hash`,
`created_at`, `merchant_id`, `status`.

Immutability here is a workflow property, not a trigger: nothing prevents an `UPDATE` at the database
level. `status`, `activated_at`, `activated_by` and `rejected_reason` are updated in place as a bundle
moves through its lifecycle; the rules themselves are not.

#### `policy_rules`

One rule: a typed predicate over facts, with an effect. `conditions` is a small JSON expression tree,
not code — it cannot call anything, loop, or reach the network, so evaluation is total and
side-effect-free.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `prl_`-prefixed ULID |
| `bundle_id` | `VARCHAR(32)` | | — | Bundle this rule belongs to |
| `priority` | `INTEGER` | | `100` | Lower runs first; the first matching `DENY` wins outright |
| `name` | `VARCHAR(160)` | | — | Rule name, shown in evaluations |
| `description` | `TEXT` | yes | — | Free text |
| `effect` | `VARCHAR(24)` | | — | `PolicyEffect` |
| `conditions` | `JSONB` | | `{}` | Expression tree over the fact namespace |
| `cap_amount_minor` | `BIGINT` | yes | — | For `CAP` rules: the absolute ceiling imposed |
| `cap_percent` | `SMALLINT` | yes | — | For `CAP` rules: the percentage ceiling imposed |
| `currency` | `VARCHAR(3)` | | `INR` | Currency of `cap_amount_minor` |
| `is_immutable` | `BOOLEAN` | | `false` | A rule the merchant cannot compile away; regulatory floors live here |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |

Primary key `pk_policy_rules (id)`. Foreign key `fk_policy_rules_bundle_id_policy_bundles →
policy_bundles.id` **`CASCADE`**. Check `ck_policy_rules_rule_cap_percent_range`
(`cap_percent IS NULL OR cap_percent BETWEEN 0 AND 100`). Composite index
`ix_rules_bundle_priority (bundle_id, priority)`; single-column indexes on `bundle_id`, `created_at`.

Not merchant-scoped: tenancy is reached through the bundle. No `updated_at`.

#### `policy_evaluations`

The recorded result of one evaluation. Append-only by construction. An action cannot execute without
one of these, and the row names the exact bundle and rule that permitted it — which is what makes "why
was this allowed?" answerable months later.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `pev_`-prefixed ULID |
| `case_id` | `VARCHAR(32)` | | — | Case evaluated |
| `action_id` | `VARCHAR(32)` | yes | — | Action evaluated |
| `bundle_id` | `VARCHAR(32)` | | — | Bundle used |
| `bundle_version` | `INTEGER` | | — | Version of that bundle, denormalised |
| `effect` | `VARCHAR(24)` | | — | `PolicyEffect` of the decision |
| `matched_rule_id` | `VARCHAR(32)` | yes | — | Rule that decided it |
| `matched_rule_name` | `VARCHAR(160)` | yes | — | That rule's name, denormalised |
| `trace` | `JSONB` | | `[]` | Every rule that fired, in order, so a surprising outcome can be traced |
| `facts` | `JSONB` | | `{}` | The exact facts evaluated, so the decision can be replayed bit for bit |
| `capped_amount_minor` | `BIGINT` | yes | — | Ceiling applied, for a `CAP` outcome |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `merchant_id` | `VARCHAR(32)` | | — | Owning tenant |

Primary key `pk_policy_evaluations (id)`. The only foreign key is to `merchants.id` `RESTRICT`.
Composite index `ix_evaluations_case_created (case_id, created_at)`; single-column indexes on
`action_id`, `bundle_id`, `case_id`, `created_at`, `effect`, `merchant_id`. No `updated_at`, and no
trigger.

#### `approvals`

A human decision the graph is blocked on. The only table with optimistic locking: two operators opening
the same item both see `version` 1; the first to resolve it writes version 2, and the second gets a
`StaleDataError` and a refreshed view rather than a silent double-approval.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `apr_`-prefixed ULID |
| `case_id` | `VARCHAR(32)` | | — | Case awaiting a decision |
| `action_id` | `VARCHAR(32)` | | — | Action awaiting a decision; unique, so one action has at most one approval |
| `thread_id` | `VARCHAR(64)` | | — | LangGraph thread blocked on this |
| `kind` | `VARCHAR(32)` | | `HUMAN_APPROVAL` | `InterruptKind` |
| `presented_summary` | `TEXT` | | — | Snapshot of what the operator was shown |
| `presented_rationale` | `TEXT` | yes | — | The planner's reasoning, verbatim |
| `presented_payload` | `JSONB` | | `{}` | Action parameters as presented |
| `amount_minor` | `BIGINT` | yes | — | Money at stake |
| `currency` | `VARCHAR(3)` | | `INR` | Amount currency |
| `escalation_reason` | `TEXT` | | — | Why a human is being asked at all — which rule escalated this |
| `requested_at` | `TIMESTAMPTZ` | | — | When the approval was requested |
| `expires_at` | `TIMESTAMPTZ` | yes | — | When the request lapses |
| `decision` | `VARCHAR(16)` | yes | — | `ApprovalDecision`; null while pending |
| `decided_at` | `TIMESTAMPTZ` | yes | — | When it was decided |
| `decided_by` | `VARCHAR(120)` | yes | — | Named operator |
| `decision_note` | `TEXT` | yes | — | Operator's note |
| `edited_payload` | `JSONB` | yes | — | Set when the operator amended rather than plainly approving |
| `version` | `INTEGER` | | `1` | Optimistic lock; `version_id_col` on the mapper |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `updated_at` | `TIMESTAMPTZ` | | `now()` | Last ORM update |
| `merchant_id` | `VARCHAR(32)` | | — | Owning tenant |

Primary key `pk_approvals (id)`. The only foreign key is to `merchants.id` `RESTRICT`. Unique
`uq_approvals_action_id (action_id)`. Composite index
`ix_approvals_pending (merchant_id, decision, requested_at)`; single-column indexes on `case_id`,
`created_at`, `decision`, `expires_at`, `merchant_id`, `requested_at`, `thread_id`.

Everything the operator needs is snapshotted at request time, so the queue renders identically no
matter what changes underneath it.

### 8.6 Communications

#### `consent_receipts`

DPDPA consent, recorded per purpose and per notice version. Consent is never general: a receipt exists
for a specific `(principal, purpose, notice_version)` triple. Withdrawal is a new row, not an update,
so the history of what was permitted when is never lost.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `cnt_`-prefixed ULID |
| `customer_id` | `VARCHAR(32)` | | — | Data principal |
| `purpose` | `VARCHAR(40)` | | — | `MessagePurpose` this consent covers |
| `state` | `VARCHAR(24)` | | — | `ConsentState` |
| `notice_version` | `VARCHAR(32)` | | — | Version of the notice shown |
| `notice_summary` | `TEXT` | yes | — | Exactly what the principal was shown |
| `granted_at` | `TIMESTAMPTZ` | yes | — | When consent was given |
| `withdrawn_at` | `TIMESTAMPTZ` | yes | — | When it was withdrawn |
| `expires_at` | `TIMESTAMPTZ` | yes | — | When it lapses |
| `collection_method` | `VARCHAR(48)` | | `checkout` | How the principal expressed it — checkout flow, preference centre, reply |
| `evidence_reference` | `VARCHAR(96)` | yes | — | Evidence handle, never the raw artifact |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `updated_at` | `TIMESTAMPTZ` | | `now()` | Last ORM update |
| `merchant_id` | `VARCHAR(32)` | | — | Owning tenant |

Primary key `pk_consent_receipts (id)`. Foreign keys to `customers.id` and `merchants.id`, both
`RESTRICT`. Check `ck_consent_receipts_granted_needs_timestamp`:
`(state <> 'granted') OR (granted_at IS NOT NULL)`. Composite index
`ix_consent_lookup (customer_id, purpose, state)`; single-column indexes on `created_at`,
`customer_id`, `expires_at`, `merchant_id`, `purpose`, `state`.

One caveat on that check constraint: `state` is an enum column, so it stores the member name `GRANTED`
in upper case, while the constraint compares against the lower-case literal `'granted'`. The comparison
therefore never matches and the constraint is satisfied by every row. The rule it expresses is enforced
in `ConsentReceipt.is_effective_at()`, which returns false for a `GRANTED` receipt with no
`granted_at`.

#### `outreach_messages`

One outbound message, from composition through delivery or suppression. Suppressed messages are stored,
not discarded: "we did not contact this customer because consent was withdrawn" is exactly the record a
regulator asks for.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `msg_`-prefixed ULID |
| `case_id` | `VARCHAR(32)` | yes | — | Case this message serves |
| `action_id` | `VARCHAR(32)` | yes | — | Action that produced it |
| `customer_id` | `VARCHAR(32)` | | — | Recipient |
| `channel` | `VARCHAR(16)` | | — | `Channel` |
| `purpose` | `VARCHAR(40)` | | — | `MessagePurpose` |
| `status` | `VARCHAR(32)` | | `QUEUED` | `DeliveryStatus` |
| `language` | `VARCHAR(8)` | | `en` | Language the body was composed in |
| `subject` | `VARCHAR(300)` | yes | — | Subject line |
| `body_redacted` | `TEXT` | | — | Rendered body with PII already tokenised; rehydrated only at send time |
| `consent_receipt_id` | `VARCHAR(32)` | yes | — | Which consent receipt authorised this send; null only when suppressed |
| `suppression_reason` | `TEXT` | yes | — | Why the send was suppressed |
| `idempotency_key` | `VARCHAR(96)` | | — | Stable across retries; unique |
| `provider_message_id` | `VARCHAR(128)` | yes | — | Channel provider's handle |
| `cost_minor` | `BIGINT` | | `0` | Channel cost in minor units |
| `queued_at` | `TIMESTAMPTZ` | | — | When it entered the queue |
| `sent_at` | `TIMESTAMPTZ` | yes | — | When it was dispatched |
| `delivered_at` | `TIMESTAMPTZ` | yes | — | When the provider confirmed delivery |
| `responded_at` | `TIMESTAMPTZ` | yes | — | When the customer responded |
| `converted` | `BOOLEAN` | | `false` | Set when the customer took the action the message asked for |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `updated_at` | `TIMESTAMPTZ` | | `now()` | Last ORM update |
| `merchant_id` | `VARCHAR(32)` | | — | Owning tenant |

Primary key `pk_outreach_messages (id)`. Foreign keys to `customers.id` and `merchants.id`, both
`RESTRICT`. Unique `uq_outreach_messages_idempotency_key`. Check
`ck_outreach_messages_message_cost_non_negative` (`cost_minor >= 0`). Composite indexes
`ix_messages_customer_sent (customer_id, sent_at)` and
`ix_messages_frequency_window (customer_id, queued_at)`; single-column indexes on `action_id`,
`case_id`, `channel`, `converted`, `created_at`, `customer_id`, `merchant_id`, `purpose`, `status`.

#### `erasure_requests`

A DPDPA right-to-erasure request, worked asynchronously with a dead-letter path. Financial records are
tombstoned rather than deleted: PII is replaced with irreversible tokens while the ledger and audit rows
stay intact.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `ers_`-prefixed ULID |
| `customer_id` | `VARCHAR(32)` | | — | Data principal |
| `status` | `VARCHAR(24)` | | `REQUESTED` | `ErasureStatus` |
| `requested_at` | `TIMESTAMPTZ` | | — | When the request arrived |
| `completed_at` | `TIMESTAMPTZ` | yes | — | When it finished |
| `targets` | `JSONB` | | `{}` | Per-target progress: which stores have been purged and which have not |
| `attempts` | `SMALLINT` | | `0` | Processing attempts made |
| `next_attempt_at` | `TIMESTAMPTZ` | yes | — | When to retry |
| `last_error` | `TEXT` | yes | — | Most recent failure |
| `dead_lettered_at` | `TIMESTAMPTZ` | yes | — | When it was given up on |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `updated_at` | `TIMESTAMPTZ` | | `now()` | Last ORM update |
| `merchant_id` | `VARCHAR(32)` | | — | Owning tenant |

Primary key `pk_erasure_requests (id)`. Foreign keys to `customers.id` and `merchants.id`, both
`RESTRICT`. Check `ck_erasure_requests_erasure_attempts_non_negative` (`attempts >= 0`). Composite
index `ix_erasure_due (status, next_attempt_at)`; single-column indexes on `created_at`, `customer_id`,
`merchant_id`, `next_attempt_at`, `status`.

#### `contact_ledger`

Append-only record of every contact, for frequency-cap arithmetic. Separate from `outreach_messages` so
the cap can be enforced with one narrow index scan, and so it survives message retention policies.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `ctl_`-prefixed ULID |
| `customer_id` | `VARCHAR(32)` | | — | Customer contacted |
| `channel` | `VARCHAR(16)` | | — | `Channel` |
| `purpose` | `VARCHAR(40)` | | — | `MessagePurpose` |
| `contacted_at` | `TIMESTAMPTZ` | | — | When contact occurred |
| `message_id` | `VARCHAR(32)` | | — | `outreach_messages.id`; not a foreign key |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `merchant_id` | `VARCHAR(32)` | | — | Owning tenant |

Primary key `pk_contact_ledger (id)`. The only foreign key is to `merchants.id` `RESTRICT`; even
`customer_id` is a plain column here, deliberately. Composite index
`ix_contact_customer_time (customer_id, contacted_at)`; single-column indexes on `contacted_at`,
`created_at`, `customer_id`, `merchant_id`. No `updated_at`, no check constraints, and no trigger.

### 8.7 Experiment

#### `recovery_batches`

A population of at-risk cases worked together under one experiment. The batch records its seed:
re-running with the same seed reproduces the same population, the same arm assignment and — in offline
mode — the same outcomes.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `bat_`-prefixed ULID |
| `name` | `VARCHAR(160)` | | — | Batch name |
| `seed` | `BIGINT` | | — | Deterministic seed for the whole run |
| `description` | `TEXT` | yes | — | Free text |
| `control_bps` | `SMALLINT` | | `1000` | Share assigned to `CONTROL`, in basis points |
| `baseline_bps` | `SMALLINT` | | `1000` | Share assigned to `BASELINE` |
| `anvil_bps` | `SMALLINT` | | `8000` | Share assigned to `ANVIL` |
| `case_count` | `INTEGER` | | `0` | Cases in the batch |
| `total_at_risk_minor` | `BIGINT` | | — | Total money at risk, minor units |
| `currency` | `VARCHAR(3)` | | `INR` | Amount currency |
| `started_at` | `TIMESTAMPTZ` | yes | — | When the run began |
| `completed_at` | `TIMESTAMPTZ` | yes | — | When it finished |
| `horizon_days` | `SMALLINT` | | `30` | Simulated horizon, so a thirty-day window runs in seconds |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `updated_at` | `TIMESTAMPTZ` | | `now()` | Last ORM update |
| `merchant_id` | `VARCHAR(32)` | | — | Owning tenant |

Primary key `pk_recovery_batches (id)`. Foreign key to `merchants.id` `RESTRICT`. Checks
`ck_recovery_batches_batch_arms_sum_to_one` (`control_bps + baseline_bps + anvil_bps = 10000`),
`ck_recovery_batches_batch_seed_positive` (`seed > 0`) and
`ck_recovery_batches_batch_horizon_positive` (`horizon_days > 0`). Indexes on `created_at`,
`merchant_id`.

The arms-sum check is what stops a split that quietly loses or double-counts cases.

#### `arm_assignments`

Which arm a case landed in, and the hash that put it there. Assignment is a pure function of
`(batch seed, case id)`; storing the hash means anyone can recompute the assignment and confirm it was
not chosen after the fact to flatter the result.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `asg_`-prefixed ULID |
| `batch_id` | `VARCHAR(32)` | | — | Batch this assignment belongs to |
| `case_id` | `VARCHAR(32)` | | — | Case assigned; unique, so a case is assigned once |
| `arm` | `VARCHAR(16)` | | — | `ExperimentArm` |
| `assignment_hash` | `VARCHAR(64)` | | — | Hash that produced the assignment |
| `bucket` | `SMALLINT` | | — | Position of the hash in `[0, 10000)`, for auditing the split |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |

Primary key `pk_arm_assignments (id)`. No foreign keys. Unique `uq_arm_assignments_case_id (case_id)`.
Check `ck_arm_assignments_assignment_bucket_range` (`bucket BETWEEN 0 AND 9999`). Composite index
`ix_assignments_batch_arm (batch_id, arm)`; single-column indexes on `arm`, `batch_id`, `created_at`.
No `updated_at`.

#### `batch_results`

Computed outcome for one arm of one batch. Stored rather than recomputed on every dashboard load, and
stored *per arm* so lift is always a comparison of like with like.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `bres_`-prefixed ULID |
| `batch_id` | `VARCHAR(32)` | | — | Batch these results describe |
| `arm` | `VARCHAR(16)` | | — | `ExperimentArm` |
| `case_count` | `INTEGER` | | `0` | Cases in this arm |
| `recovered_count` | `INTEGER` | | `0` | Cases recovered |
| `at_risk_minor` | `BIGINT` | | — | Money at risk in this arm |
| `recovered_minor` | `BIGINT` | | — | Money recovered |
| `concession_minor` | `BIGINT` | | — | Money conceded |
| `channel_cost_minor` | `BIGINT` | | — | Outreach cost |
| `model_cost_minor` | `BIGINT` | | — | LLM cost |
| `currency` | `VARCHAR(3)` | | `INR` | Currency of every amount on the row |
| `recovery_rate_bps` | `INTEGER` | | `0` | Recovery rate in basis points |
| `recovery_rate_ci_low_bps` | `INTEGER` | | `0` | Bootstrap interval, lower bound |
| `recovery_rate_ci_high_bps` | `INTEGER` | | `0` | Bootstrap interval, upper bound |
| `lift_vs_control_bps` | `INTEGER` | yes | — | Lift over control; populated for non-control arms only |
| `lift_ci_low_bps` | `INTEGER` | yes | — | Lift interval, lower bound |
| `lift_ci_high_bps` | `INTEGER` | yes | — | Lift interval, upper bound |
| `is_significant` | `BOOLEAN` | yes | — | False when the interval straddles zero; displayed honestly either way |
| `by_failure_class` | `JSONB` | | `{}` | Recovery broken out by failure class, so a headline number cannot hide one class doing all the work |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |

Primary key `pk_batch_results (id)`. No foreign keys. Unique `uq_result_batch_arm (batch_id, arm)`.
Checks `ck_batch_results_result_recovered_lte_cases` (`recovered_count <= case_count`) and
`ck_batch_results_result_recovered_non_negative` (`recovered_minor >= 0`). Indexes on `batch_id`,
`created_at`. No `updated_at`.

### 8.8 Platform

#### `domain_events`

The append-only event log. **Enforced by trigger.** Written in the same transaction as the state change
it describes, so the log can never disagree with the state — which buys event sourcing's replay
guarantees without its eventual consistency.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `evt_`-prefixed ULID |
| `sequence` | `BIGINT` | | identity | Global monotonic ordering, `GENERATED ALWAYS AS IDENTITY`; assigned by the database, never by the application |
| `merchant_id` | `VARCHAR(32)` | yes | — | Tenant, where the event has one |
| `aggregate_type` | `VARCHAR(48)` | | — | Kind of entity the event is about |
| `aggregate_id` | `VARCHAR(32)` | | — | That entity's id |
| `event_type` | `VARCHAR(64)` | | — | Event name, free text |
| `aggregate_version` | `INTEGER` | | `1` | Per-aggregate version, for detecting out-of-order application |
| `payload` | `JSONB` | | `{}` | Event body |
| `correlation_id` | `VARCHAR(64)` | yes | — | Correlation across a whole recovery journey |
| `causation_id` | `VARCHAR(64)` | yes | — | Causation to the direct parent |
| `occurred_at` | `TIMESTAMPTZ` | | — | Business time of the event |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |

Primary key `pk_domain_events (id)`. No foreign keys. Unique
`uq_event_aggregate_version (aggregate_type, aggregate_id, aggregate_version)` and a unique index on
`sequence`. Composite index `ix_events_aggregate_seq (aggregate_type, aggregate_id, sequence)`;
single-column indexes on `aggregate_id`, `aggregate_type`, `correlation_id`, `created_at`,
`event_type`, `merchant_id`, `occurred_at`.

#### `outbox`

Transactional outbox. Written with the state change, relayed afterwards, which is why a worker crash
cannot lose a scheduled action: the intent and the fact that justified it commit together or not at
all. This table is mutable — the relay claims, publishes and retries rows.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `obx_`-prefixed ULID |
| `topic` | `VARCHAR(64)` | | — | Destination topic |
| `payload` | `JSONB` | | `{}` | Message body |
| `partition_key` | `VARCHAR(64)` | yes | — | Messages sharing a key are relayed in order |
| `available_at` | `TIMESTAMPTZ` | | — | Earliest time the message may be relayed |
| `claimed_at` | `TIMESTAMPTZ` | yes | — | When a relay claimed it |
| `claimed_by` | `VARCHAR(64)` | yes | — | Which relay |
| `published_at` | `TIMESTAMPTZ` | yes | — | When it was published |
| `attempts` | `SMALLINT` | | `0` | Publish attempts made |
| `max_attempts` | `SMALLINT` | | `8` | Attempts before dead-lettering |
| `last_error` | `TEXT` | yes | — | Most recent failure |
| `dead_lettered_at` | `TIMESTAMPTZ` | yes | — | When it was given up on |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `updated_at` | `TIMESTAMPTZ` | | `now()` | Last ORM update |

Primary key `pk_outbox (id)`. No foreign keys, and not merchant-scoped. Check
`ck_outbox_outbox_attempts_non_negative` (`attempts >= 0`). Composite index
`ix_outbox_claimable (published_at, available_at)`; single-column indexes on `available_at`,
`created_at`, `dead_lettered_at`, `partition_key`, `topic`.

#### `audit_records`

Immutable compliance trail. **Enforced by trigger.** Free of raw PII by construction: redaction happens
on the way in, because a record that reached this table with a phone number in it would be a bug no
amount of careful reading later could undo.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `adt_`-prefixed ULID |
| `sequence` | `BIGINT` | | identity | Monotonic ordering, `GENERATED ALWAYS AS IDENTITY` |
| `merchant_id` | `VARCHAR(32)` | yes | — | Tenant, where the record has one |
| `case_id` | `VARCHAR(32)` | yes | — | Case, where the record has one |
| `action_id` | `VARCHAR(32)` | yes | — | Action, where the record has one |
| `event_type` | `VARCHAR(48)` | | — | `AuditEventType` |
| `actor` | `VARCHAR(120)` | | — | Who caused it: `agent`, `system`, or an operator identity |
| `actor_kind` | `VARCHAR(16)` | | `system` | One of `agent`, `system`, `operator`, `customer` |
| `summary` | `TEXT` | | — | One-line description |
| `detail` | `JSONB` | | `{}` | Structured detail, already redacted |
| `thread_id` | `VARCHAR(64)` | yes | — | Graph thread, enabling time travel from an audit row into the state that produced it |
| `checkpoint_id` | `VARCHAR(64)` | yes | — | The specific checkpoint |
| `occurred_at` | `TIMESTAMPTZ` | | — | Business time of the event |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |

Primary key `pk_audit_records (id)`. No foreign keys. Unique index on `sequence`. Check
`ck_audit_records_audit_actor_kind_valid`
(`actor_kind IN ('agent','system','operator','customer')`). Composite indexes
`ix_audit_case_seq (case_id, sequence)` and
`ix_audit_merchant_type_time (merchant_id, event_type, occurred_at)`; single-column indexes on
`action_id`, `actor`, `case_id`, `created_at`, `event_type`, `merchant_id`, `occurred_at`, `thread_id`.

#### `processed_webhooks`

Webhook deduplication. The unique constraint on `event_id` *is* the idempotency mechanism: a duplicate
delivery raises a constraint violation, which the handler translates into a plain `200 OK` without
re-running anything.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `whk_`-prefixed ULID |
| `event_id` | `VARCHAR(128)` | | — | Razorpay's `x-razorpay-event-id` header; unique |
| `event_type` | `VARCHAR(64)` | | — | Webhook event name |
| `event_timestamp` | `TIMESTAMPTZ` | | — | Payload timestamp, checked against the replay tolerance window |
| `received_at` | `TIMESTAMPTZ` | | — | When Anvil received it |
| `processed_at` | `TIMESTAMPTZ` | yes | — | When processing completed |
| `body_digest` | `VARCHAR(64)` | | — | Digest of the raw body, so a replay with a mutated body is detectable |
| `entity_id` | `VARCHAR(64)` | yes | — | Gateway entity the event concerns |
| `processing_error` | `TEXT` | yes | — | Failure detail |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |

Primary key `pk_processed_webhooks (id)`. No foreign keys. Unique
`uq_processed_webhooks_event_id (event_id)`. Composite index
`ix_webhooks_type_received (event_type, received_at)`; single-column indexes on `created_at`,
`entity_id`, `event_type`, `received_at`. No `updated_at`, and no trigger.

The replay tolerance window is `ANVIL_WEBHOOK_TOLERANCE_SECONDS`, default 300.

#### `idempotency_records`

Server-side record of a caller-supplied idempotency key, covering Anvil's own API surface. Outbound
keys to Razorpay are recorded on the action and attempt rows instead, where they belong to the thing
they protect.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `idm_`-prefixed ULID |
| `key` | `VARCHAR(96)` | | — | Caller-supplied key; unique |
| `scope` | `VARCHAR(64)` | | — | Endpoint or operation the key applies to |
| `request_digest` | `VARCHAR(64)` | | — | Digest of the request, so a reused key with a different body is detectable |
| `response_status` | `SMALLINT` | yes | — | Status code returned the first time |
| `response_body` | `JSONB` | yes | — | Body returned the first time, replayed on a duplicate |
| `completed_at` | `TIMESTAMPTZ` | yes | — | When the original request finished |
| `expires_at` | `TIMESTAMPTZ` | | — | When the record may be reaped |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |
| `updated_at` | `TIMESTAMPTZ` | | `now()` | Last ORM update |

Primary key `pk_idempotency_records (id)`. No foreign keys, and not merchant-scoped. Unique
`uq_idempotency_records_key (key)`. Indexes on `created_at`, `expires_at`, `scope`.

#### `llm_calls`

Every model call, recorded, with prompts stored redacted. Cost and latency here feed the "cost per
recovered rupee" figure the evidence harness reports, so the economics of the agent are measured rather
than asserted.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `VARCHAR(32)` | | — | Primary key, `llm_`-prefixed ULID |
| `case_id` | `VARCHAR(32)` | yes | — | Case the call served |
| `kind` | `VARCHAR(24)` | | — | `LLMCallKind` |
| `model` | `VARCHAR(64)` | | — | Model identifier |
| `prompt_redacted` | `TEXT` | | — | Prompt, redacted before persistence, always |
| `response_raw` | `TEXT` | yes | — | Raw response text |
| `parsed_output` | `JSONB` | yes | — | Validated structured output |
| `input_tokens` | `INTEGER` | | `0` | Prompt tokens |
| `output_tokens` | `INTEGER` | | `0` | Completion tokens |
| `cost_minor` | `BIGINT` | | `0` | Call cost in minor units |
| `latency_ms` | `INTEGER` | | `0` | Wall-clock latency |
| `attempt` | `SMALLINT` | | `1` | Retry ordinal |
| `succeeded` | `BOOLEAN` | | `true` | Whether the call produced valid output |
| `validation_error` | `TEXT` | yes | — | Why validation failed |
| `from_fixture` | `BOOLEAN` | | `false` | True when the response came from a recorded fixture rather than the API |
| `fixture_key` | `VARCHAR(96)` | yes | — | Which fixture |
| `created_at` | `TIMESTAMPTZ` | | `now()` | Row creation |

Primary key `pk_llm_calls (id)`. No foreign keys, and not merchant-scoped. Checks
`ck_llm_calls_llm_tokens_non_negative` (`input_tokens >= 0 AND output_tokens >= 0`) and
`ck_llm_calls_llm_cost_non_negative` (`cost_minor >= 0`). Composite index
`ix_llm_calls_kind_created (kind, created_at)`; single-column indexes on `case_id`, `created_at`,
`fixture_key`, `from_fixture`, `kind`, `model`, `succeeded`. No `updated_at`.

---

## 9. Append-only tables

Eleven tables have no `updated_at` column, because they carry `CreatedAtMixin` rather than
`TimestampMixin` and have no update path in the application:

`arm_assignments`, `audit_records`, `batch_results`, `contact_ledger`, `domain_events`,
`ledger_entries`, `ledger_transactions`, `llm_calls`, `policy_evaluations`, `policy_rules`,
`processed_webhooks`.

For four of those, the rule is enforced by Postgres rather than by convention.

### The immutability guard

Migration `9a1b2c3d4e5f` installs `anvil_reject_ledger_mutation()`, a `plpgsql` trigger function, and
binds it `BEFORE UPDATE OR DELETE ... FOR EACH ROW` to four tables:

| Table | Trigger |
|---|---|
| `ledger_entries` | `trg_ledger_entries_immutable` |
| `ledger_transactions` | `trg_ledger_transactions_immutable` |
| `domain_events` | `trg_domain_events_immutable` |
| `audit_records` | `trg_audit_records_immutable` |

The function raises with `ERRCODE = 'restrict_violation'` and the message
`ledger is append-only: <op> on <table> is refused. Post a reversal instead.`, plus a hint naming the
correct remedy. Application-level enforcement protects only the paths that go through the application;
a migration that "just fixes one row", a psql session at 2am, or a future module that adds its own
writer would all bypass it. The trigger does not care which client is connected.

The DDL lives in `anvil/ledger/immutability.py` as `LEDGER_IMMUTABILITY_DDL` and
`LEDGER_IMMUTABILITY_DOWN_DDL`, with `apply()` and `remove()` coroutines the test fixtures use. The
`CREATE OR REPLACE FUNCTION` plus `DROP TRIGGER IF EXISTS` structure makes it idempotent: safe to run
repeatedly. `PROTECTED_TABLES` names the four tables as a tuple for anything that needs to assert on
the set.

### The escape hatch

The function returns normally — permitting the mutation — when the session GUC
`anvil.allow_ledger_mutation` is set to `on`:

```sql
SET LOCAL anvil.allow_ledger_mutation = 'on';
```

Nothing in Anvil ever sets it. It exists because a genuine disaster-recovery operation must be
*possible*: an immutability rule with no documented override gets dropped in a panic, which is strictly
worse than one that must be turned on explicitly, leaves the intent in the session settings, and can be
alerted on.

### What the triggers do not cover

`INSERT` is untouched — these tables are append-only, not read-only. `TRUNCATE` is not intercepted,
because the triggers are row-level. The other seven `CreatedAtMixin` tables have no trigger; their
append-only character is a property of the code, not of the database.

---

## 10. Migration workflow

### Alembic configuration

`alembic.ini` sets `script_location = alembic`, `prepend_sys_path = .`, `version_path_separator = os`
and `timezone = UTC`. It deliberately contains **no** `sqlalchemy.url`: `alembic/env.py` sets it at
runtime from `Settings.sync_database_url`, so migrations follow `ANVIL_DATABASE_URL` and never drift
from application configuration.

`env.py` runs migrations synchronously against psycopg while the application uses asyncpg. Alembic is a
deploy-time tool; a synchronous driver keeps the migration path simple to reason about. It imports
`anvil.db.models` to register all 32 tables on `Base.metadata`, and configures both offline and online
modes with `compare_type=True` and `compare_server_default=True`, so a changed column type or DDL
default is detected rather than silently ignored. The online path uses `pool.NullPool`.

### Revision history

| Revision | Down revision | Contents |
|---|---|---|
| `8c4dce6e89c7` | — | Initial schema: all 32 tables, their constraints and indexes. Autogenerated |
| `9a1b2c3d4e5f` | `8c4dce6e89c7` | Ledger immutability guard: the trigger function and four triggers. Hand-written |

`head` is `9a1b2c3d4e5f`.

### Applying migrations

The Makefile target is the supported path:

```bash
make db-up      # docker compose up -d postgres redis
make db-wait    # block until pg_isready -U anvil -d anvil succeeds
make migrate    # .venv/bin/alembic upgrade head
```

`make demo` chains `db-up`, `db-wait`, `migrate` and `seed` before bringing the rest of the stack up.
`make test-all` chains `db-up`, `db-wait` and `migrate` before running the full test suite, so
integration and e2e tests always run against a migrated database.

Running Alembic directly does the same thing:

```bash
.venv/bin/alembic upgrade head          # apply everything outstanding
.venv/bin/alembic upgrade +1            # apply exactly one revision
.venv/bin/alembic current               # which revision the database is on
.venv/bin/alembic history --verbose     # the full chain
.venv/bin/alembic upgrade head --sql    # emit SQL instead of executing it
```

### Creating a migration

There is no Makefile target for authoring a revision; use Alembic directly.

```bash
# 1. Change the model, and re-export it from anvil/db/models/__init__.py if it is new.
# 2. Generate the revision.
.venv/bin/alembic revision --autogenerate -m "add settlement lag to payment attempts"

# 3. Read the generated file. Autogenerate proposes; it does not decide.
# 4. Apply it.
.venv/bin/alembic upgrade head

# 5. Confirm the models and the database now agree: this must produce an empty migration.
.venv/bin/alembic revision --autogenerate -m "should be empty"
```

For a change autogenerate cannot see, create an empty revision and write the DDL by hand:

```bash
.venv/bin/alembic revision -m "install some trigger"
```

Four things about autogenerate in this repository:

- **A model that is not re-exported from `anvil/db/models/__init__.py` is invisible.** Autogenerate will
  see its table in the database, not in the metadata, and propose to drop it.
- **Triggers and functions are invisible.** Alembic compares tables, columns, indexes and constraints.
  Revision `9a1b2c3d4e5f` is hand-written for exactly this reason, and any change to the immutability
  guard must be too.
- **The generated file imports `anvil.db.base`,** because `UTCDateTime` and `CurrencyType` appear in
  column definitions. `alembic/script.py.mako` includes that import in every new revision.
- **Constraint names come from the naming convention,** so a generated migration should never contain an
  unnamed constraint. If one appears, the model declared it in a way that bypassed the convention.

### Rolling back

```bash
.venv/bin/alembic downgrade -1              # undo the most recent revision
.venv/bin/alembic downgrade 8c4dce6e89c7    # go back to a named revision
.venv/bin/alembic downgrade base            # undo everything; drops all 32 tables
```

Both revisions implement `downgrade()`. `9a1b2c3d4e5f` drops the four triggers and the function;
`8c4dce6e89c7` drops every index and table in reverse dependency order. Downgrading past
`8c4dce6e89c7` destroys all data.

To discard the local database entirely and start from an empty volume:

```bash
make clean      # docker compose down -v
make demo       # recreate, migrate and reseed
```

### Seeding

```bash
make seed       # .venv/bin/python -m anvil.simulator.seed
```

The seeder builds a deterministic demo world. Because entity ids come from `deterministic_id()` and the
run is driven by `ANVIL_SEED` (default `20260902`), reseeding with the same seed produces a
byte-identical database.
