# Anvil — HTTP API

The API is one FastAPI application, `anvil.main_api:app`. It serves twelve JSON endpoints and the
single-page console that consumes them, from one process on one port.

It needs no database and no credentials. All state comes from a seeded simulator built in process at
startup ([`anvil/api/state.py`](../../anvil/api/state.py)), including a live approval queue of genuinely
paused LangGraph threads — each queue item is a graph that ran to an `interrupt` and is sitting on a
committed checkpoint. Approving one resumes that graph, which then runs authorisation, policy and the
executor for real.

Everything below is the behaviour of the code in [`anvil/api/`](../../anvil/api/), verified against a
running instance.

---

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m uvicorn anvil.main_api:app --port 8000
```

Startup builds a 900-customer population, opens its cases, and then runs recovery graphs until eight
of them have paused for a human. That takes a few seconds; the process logs `anvil_api_ready` with
the seed, the at-risk case count and the queue depth when it is ready to serve.

Add `--reload` for development. There is no Make target that runs the API directly — `make demo` boots
it through Docker Compose.

### Docker

The image's `CMD` is the same command:

```bash
docker build -t anvil .
docker run --rm -p 8000:8000 anvil
```

Under Compose, the `api` service publishes port 8000 and passes `ANVIL_MODE`, `ANVIL_SEED` and the
credential variables through from the environment:

```bash
docker compose up api
```

Two caveats, both honest: `docker compose up` with no service argument also builds the `console`
service from `./console`, a directory that is not in this tree, so it fails — name the service. And
per the root README, the Compose file is committed but unverified; local development runs the venv
command above against a native Postgres.

### Base URL and documented surface

| Path | What it is |
| --- | --- |
| `http://localhost:8000` | Base URL. Every endpoint below is relative to it. |
| `/docs` | Swagger UI, generated from the route signatures. |
| `/openapi.json` | The OpenAPI 3.1 schema. |
| `/redoc` | **Disabled** (`redoc_url=None`) — returns 404. |
| `/` | The console page (see [The console](#the-console)). |
| `/static/…` | The `anvil/api/static` directory, mounted read-only. |

---

## Configuration

Settings come from [`anvil/core/config.py`](../../anvil/core/config.py) with the `ANVIL_` prefix, read
from the environment or a `.env` file. Every value has a working default. The ones that change what
this process does:

| Variable | Default | Effect on the API |
| --- | --- | --- |
| `ANVIL_MODE` | `offline` | `offline` needs no credentials and reports `model: "offline fixtures"` on `/health`. `live` requires all four credentials below and fails to start without them. |
| `ANVIL_SEED` | `20260902` | Seeds the console's world. Same seed, same cases, same queue, same case ids. Must be positive. |
| `ANVIL_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `ANVIL_LOG_FORMAT` | `console` | `console` or `json`. |
| `ANVIL_ENV` | `local` | `local`, `ci`, `demo`. |
| `ANVIL_MODEL_CLASSIFIER` / `ANVIL_MODEL_PLANNER` | `claude-sonnet-5` / `claude-opus-5` | Only reported by `/health`, and only outside offline mode. |
| `ANVIL_RAZORPAY_KEY_ID`, `ANVIL_RAZORPAY_KEY_SECRET`, `ANVIL_RAZORPAY_WEBHOOK_SECRET`, `ANVIL_ANTHROPIC_API_KEY` | empty | Required only when `ANVIL_MODE=live`. |

`ANVIL_DATABASE_URL` and `ANVIL_REDIS_URL` are read by the settings object but not used by this
process — the console's state is in memory.

**There is no authentication.** No API key, no session, no CORS configuration, no rate limit. Every
endpoint is open to anyone who can reach the port, and `POST /api/approvals/{approval_id}` mutates
server state. Bind it to localhost or put it behind something.

---

## Conventions

**Money is an object, twice over.** Every monetary value is rendered as:

```json
{ "minor": 149900, "currency": "INR", "display": "₹1,499.00" }
```

`minor` is an integer count of minor units (paise) and is the only field to do arithmetic on.
`display` is pre-formatted with Indian digit grouping (`₹12,34,567.89`), so clients never reimplement
it and never disagree about it. `currency` is `INR` or `USD`.

**Rates and probabilities are basis points.** `rate_bps`, `probability_bps`, `confidence_bps`,
`difference_bps` — integers where 10000 = 100%. Scores (`recovery_likelihood`, `churn_risk`) are
0–1000. No floats cross the wire except `z_score`.

**Two kinds of timestamp.** Machine fields (`at`, timeline `at`) are ISO-8601 with a UTC offset.
Human fields (`ist_label`, `requested_at`, `failed_at` on a case) are pre-formatted IST display
strings such as `Wed 30 Sep 10:30 IST`.

**The world is fixed at startup, and mutations are in memory.** The case list and the approval queue
are rebuilt on every restart from the seed; approvals resolved in one run are pending again in the
next.

---

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Liveness, plus the run mode, seed and model configuration. |
| GET | `/api/taxonomy` | The decline-code taxonomy: 76 codes across four namespaces, and the retry posture of each failure class. |
| POST | `/api/classify` | Run the deterministic classifier on arbitrary signals and see whether it resolves or escalates. |
| GET | `/api/scheduler/explain` | Solve the retry-timing decision and return the candidate hours that lost. |
| GET | `/api/policy/bundle` | The live policy bundle: every rule, its condition in English, its content hash. |
| POST | `/api/policy/evaluate` | Evaluate an arbitrary fact set against that bundle, with the full rule trace. |
| GET | `/api/ledger/demo` | Build a real posting sequence and show that every transaction balances. Nothing is written. |
| GET | `/api/cases` | The at-risk book. |
| GET | `/api/cases/{case_id}` | One case with its timeline, actions and ledger drafts. |
| GET | `/api/approvals` | Everything waiting on a person. Each item is a paused graph. |
| POST | `/api/approvals/{approval_id}` | Resume that graph with a human decision. |
| GET | `/api/batch` | Run the seeded three-arm experiment and return the evidence report. |

---

## System

### GET /health

No parameters.

| Field | Type | Notes |
| --- | --- | --- |
| `status` | string | Always `"ok"` — the endpoint has no failure branch. |
| `mode` | string | `offline` or `live`. |
| `version` | string | `"1.0.0"`, hardcoded alongside the FastAPI app version. |
| `database` | string | Constant explanatory string; this process uses none. |
| `model` | string | `"offline fixtures"` offline, otherwise `"{classifier} / {planner}"`. |
| `seed` | integer | The seed the served world was built from. |

```bash
curl -s localhost:8000/health
```

```json
{
  "status": "ok",
  "mode": "offline",
  "version": "1.0.0",
  "database": "not required — the console runs from the seeded simulator",
  "model": "offline fixtures",
  "seed": 20260902
}
```

Errors: none. If the process is up, this returns 200.

---

### GET /api/taxonomy

No parameters. The decline taxonomy, browsable.

| Field | Type | Notes |
| --- | --- | --- |
| `total_codes` | integer | 76 with the shipped tables. |
| `by_namespace` | object&lt;string, integer&gt; | Codes per namespace: `upi` 20, `nach` 13, `card` 18, `text` 25. |
| `classes` | array&lt;FailureClassView&gt; | One entry per failure class, in enum order (10 of them). |

**FailureClassView**

| Field | Type | Notes |
| --- | --- | --- |
| `failure_class` | string | One of `insufficient_funds`, `instrument_expired`, `issuer_technical`, `limit_exceeded`, `mandate_revoked`, `mandate_paused`, `account_closed`, `risk_declined`, `auth_required`, `unknown`. |
| `posture` | string | `retry_fast`, `retry_scheduled`, `retry_once`, `deferred`, `never`. |
| `retryable` | boolean | From the class's retry curve. |
| `max_attempts` | integer | 0 for terminal classes. |
| `rationale` | string | Why this class behaves the way it does. |
| `example_codes` | array&lt;string&gt; | Up to 8, sorted, drawn from every namespace. |

```bash
curl -s localhost:8000/api/taxonomy
```

```json
{
  "total_codes": 76,
  "by_namespace": { "upi": 20, "nach": 13, "card": 18, "text": 25 },
  "classes": [
    {
      "failure_class": "insufficient_funds",
      "posture": "retry_scheduled",
      "retryable": true,
      "max_attempts": 4,
      "rationale": "Balance recovers on a payday rhythm, not a clock. Retrying an hour later almost always fails; retrying on the 1st or the last working day is where the money is. Timing dominates attempt count for this class.",
      "example_codes": ["01", "51", "Z9", "insufficient_balance", "insufficient_funds"]
    }
  ]
}
```

*(One class shown; the response carries all ten.)*

Errors: none.

---

### POST /api/classify

Runs the deterministic classifier and reports whether it had to give up. This is the endpoint that
makes "rules first, the model only where rules genuinely fail" checkable rather than asserted.

**Body** — every field is optional; sending `{}` is legal and resolves nothing.

| Field | Type | Notes |
| --- | --- | --- |
| `raw_code` | string \| null | The issuer or gateway reason code, e.g. `U30`, `51`, `Z9`. |
| `gateway_description` | string \| null | Free text from the gateway. |
| `bank_narration` | string \| null | Free text from the bank statement. |
| `rail_hint` | string \| null | Folded onto a code namespace. `upi`/`upi_autopay`/`autopay`/`upi_mandate`/`npci` → UPI; `nach`/`enach`/`e_nach`/`emandate`/`e_mandate`/`ach` → NACH; `card`/`cards`/`card_mandate`/`si` → card. Anything else is ignored rather than guessed. |

**Response**

| Field | Type | Notes |
| --- | --- | --- |
| `resolved` | boolean | True when the code tables settled it without a model. |
| `failure_class` | string \| null | Set only when `resolved`. |
| `confidence_bps` | integer \| null | Set only when `resolved`. |
| `matched_code` | string \| null | The normalised code that matched. |
| `matched_namespace` | string \| null | `upi`, `nach`, `card` or `text`. |
| `escalation_reason` | string \| null | When unresolved: `no_recognised_signal`, `weak_evidence` or `conflicting_signals`. |
| `would_call_model` | boolean | The inverse of `resolved` — whether this input is one the LLM exists to handle. |
| `detail` | object | `{"describes": "…"}` when resolved; `{"candidates": [{"failure_class", "confidence_bps"}]}` when not. |

```bash
curl -s -X POST localhost:8000/api/classify \
  -H 'content-type: application/json' \
  -d '{"raw_code":"U30"}'
```

```json
{
  "resolved": true,
  "failure_class": "issuer_technical",
  "confidence_bps": 9000,
  "matched_code": "u30",
  "matched_namespace": "upi",
  "escalation_reason": null,
  "would_call_model": false,
  "detail": { "describes": "issuer_technical from 'u30' (upi, via raw_code)" }
}
```

The escalating case — a code no table knows, with narration that only hints:

```bash
curl -s -X POST localhost:8000/api/classify \
  -H 'content-type: application/json' \
  -d '{"raw_code":"XZ99","bank_narration":"A/c bal low"}'
```

```json
{
  "resolved": false,
  "failure_class": null,
  "confidence_bps": null,
  "matched_code": null,
  "matched_namespace": null,
  "escalation_reason": "weak_evidence",
  "would_call_model": true,
  "detail": { "candidates": [{ "failure_class": "insufficient_funds", "confidence_bps": 5000 }] }
}
```

**Errors** — `422` if a field is the wrong type. Unrecognised input is not an error; it is the
`resolved: false` answer.

---

## Insight

### GET /api/scheduler/explain

Solves the retry decision for a hypothetical failure and returns the ranked candidates it beat.

| Parameter | Type | Default | Constraint |
| --- | --- | --- | --- |
| `failure_class` | enum | — | **Required.** One of the ten failure classes. |
| `amount_minor` | integer | `149900` | `> 0`. Amount at risk, in paise. |
| `failed_at` | datetime | `2026-09-18T06:00:00Z` | ISO-8601. A naive value is read as UTC. Also used as "now", so the solve starts from the failure. |
| `attempts_used` | integer | `0` | `>= 0`. |
| `mandate_attempts_remaining` | integer \| null | `null` | `>= 0`. Null means the class's own curve bounds the attempts. |

**Response**

| Field | Type | Notes |
| --- | --- | --- |
| `should_retry` | boolean | False for a refusal. |
| `failure_class` | string | Echoes the parameter. |
| `posture` | string | The retry posture applied. |
| `attempt_number` | integer | Which attempt this would be. |
| `attempts_remaining` | integer | After this one. |
| `at` | string \| null | ISO-8601 instant of the chosen hour; null on refusal. |
| `ist_label` | string \| null | The same instant as `Wed 30 Sep 10:30 IST`. |
| `probability_bps` | integer | Settlement probability at the chosen hour; 0 on refusal. |
| `expected_value` | Amount | Value of the remaining attempts; zero on refusal. |
| `explanation` | string | The decision in a sentence, including why that hour. |
| `refusal_reason` | string \| null | Non-null exactly when `should_retry` is false. |
| `rationale` | string | Why this failure class behaves the way it does. |
| `ranked` | array&lt;RankedHour&gt; | The top 24 candidate hours by expected value, ties broken earliest-first. **Empty on refusal.** |

**RankedHour**: `at` (ISO-8601), `ist_label` (string), `probability_bps` (integer), `value` (Amount),
`is_chosen` (boolean).

```bash
curl -s 'localhost:8000/api/scheduler/explain?failure_class=insufficient_funds'
```

```json
{
  "should_retry": true,
  "failure_class": "insufficient_funds",
  "posture": "retry_scheduled",
  "attempt_number": 1,
  "attempts_remaining": 4,
  "at": "2026-09-30T05:00:00+00:00",
  "ist_label": "Wed 30 Sep 10:30 IST",
  "probability_bps": 5275,
  "expected_value": { "minor": 128148, "currency": "INR", "display": "₹1,281.48" },
  "explanation": "Attempt 1 scheduled in 11d 23h, at Wed 30 Sep 10:30 IST, at 52.8% expected success, because the 30th sits on the salary-credit peak, when balances recover; 10:00 IST is clear of the overnight issuer maintenance window.",
  "refusal_reason": null,
  "rationale": "Balance recovers on a payday rhythm, not a clock. …",
  "ranked": [
    {
      "at": "2026-09-30T05:00:00+00:00",
      "ist_label": "Wed 30 Sep 10:30 IST",
      "probability_bps": 5275,
      "value": { "minor": 128148, "currency": "INR", "display": "₹1,281.48" },
      "is_chosen": true
    }
  ]
}
```

*(First ranked hour shown; 24 are returned.)*

A refusal, for a class whose posture is `never`:

```bash
curl -s 'localhost:8000/api/scheduler/explain?failure_class=instrument_expired'
```

```json
{
  "should_retry": false,
  "posture": "never",
  "attempts_remaining": 0,
  "at": null,
  "ist_label": null,
  "probability_bps": 0,
  "expected_value": { "minor": 0, "currency": "INR", "display": "₹0.00" },
  "refusal_reason": "instrument_expired is never worth retrying. The card will be just as expired tomorrow. The only recovery is a new instrument, so every retry attempt spent here is pure waste.",
  "ranked": []
}
```

*(Trimmed to the fields that differ.)*

**Errors**

| Status | When |
| --- | --- |
| `422` | `failure_class` missing or not a member of the enum; `amount_minor <= 0`; `attempts_used < 0`; an unparseable `failed_at`. The body names the offending parameter and lists the accepted values. |

---

### GET /api/policy/bundle

No parameters. The default bundle from [`anvil/policy/defaults.py`](../../anvil/policy/defaults.py),
evaluated in priority order.

| Field | Type | Notes |
| --- | --- | --- |
| `id` | string | `pol_default`. |
| `version` | integer | Bundle version. |
| `content_hash` | string | SHA-256 over the rule content. Changes when any rule changes. |
| `rule_count` | integer | 27 in the shipped bundle. |
| `immutable_count` | integer | 5 — the rules a merchant cannot edit away. |
| `rules` | array&lt;PolicyRuleView&gt; | In evaluation order. |

**PolicyRuleView**

| Field | Type | Notes |
| --- | --- | --- |
| `id` | string | Prefixed ULID. |
| `name` | string | Stable slug, e.g. `no-consent-no-contact`. |
| `priority` | integer | Lower runs first. |
| `effect` | string | `allow`, `deny`, `require_approval`, `cap`. |
| `description` | string \| null | Doubles as the decision's `reason` when this rule matches. |
| `condition` | string | The expression tree rendered as English. |
| `cap_amount` | Amount \| null | Absolute ceiling, for `cap` rules that set one. |
| `cap_percent` | integer \| null | Percentage ceiling, resolved against subscription MRR. |
| `is_immutable` | boolean | |

```bash
curl -s localhost:8000/api/policy/bundle
```

```json
{
  "id": "pol_default",
  "version": 1,
  "content_hash": "7023ab4499ea6cce22a980b4dbf5e781d9e4f9136c181a5aafa6f75702b69083",
  "rule_count": 27,
  "immutable_count": 5,
  "rules": [
    {
      "id": "prl_6CVPMX0SXEH5ECH7S9BJ9E17AB",
      "name": "consent-withdrawn-blocks-outreach",
      "priority": 10,
      "effect": "deny",
      "description": "The data principal has withdrawn or let lapse their consent for this purpose. Under the DPDPA no further processing for that purpose is lawful, so the message is refused rather than merely deprioritised.",
      "condition": "(is_outreach == true and consent_state in ['withdrawn', 'expired'])",
      "cap_amount": null,
      "cap_percent": null,
      "is_immutable": true
    }
  ]
}
```

*(First rule shown; 27 are returned.)*

Errors: none.

---

### POST /api/policy/evaluate

Evaluates an arbitrary fact set against that bundle. The evaluator is pure and total: the same facts
always produce the same decision, the first matching DENY stops evaluation, `require_approval` is
sticky, the tightest cap wins, and **no match denies**.

**Body** — a `PolicyFacts` object ([`anvil/policy/facts.py`](../../anvil/policy/facts.py)). `action_type`
is the only required field; everything else defaults. Unknown keys are rejected (`extra="forbid"`), so
a typo is a 422 rather than a rule that silently never matches.

| Field | Type | Default | Range / vocabulary |
| --- | --- | --- | --- |
| `action_type` | string | **required** | `retry_debit`, `split_debit`, `request_instrument_update`, `send_payment_link`, `request_mandate_reauth`, `trigger_step_up`, `send_reminder`, `send_dunning_notice`, `grant_grace_period`, `offer_partial_payment`, `offer_plan_downgrade`, `offer_winback_discount`, `escalate_to_human`, `stop_and_write_off`, `mark_churned` |
| `amount_minor` | integer | `0` | `>= 0` |
| `currency` | string | `INR` | `INR`, `USD` |
| `failure_class` | string \| null | `null` | the ten failure classes |
| `hours_since_failure` | integer | `0` | `>= 0` |
| `case_attempt_count` | integer | `0` | `>= 0` |
| `mandate_cycle_attempt_count` | integer | `0` | `>= 0` |
| `case_contact_count` | integer | `0` | `>= 0` |
| `contacts_last_24h` | integer | `0` | `>= 0` |
| `contacts_last_7d` | integer | `0` | `>= 0` |
| `hours_since_last_contact` | integer | `8760` | `0–8760`; 8760 is the "never contacted" sentinel |
| `local_hour_ist` | integer | `12` | `0–23` |
| `local_day_of_month_ist` | integer | `1` | `1–31` |
| `customer_tenure_days` | integer | `0` | `>= 0` |
| `lifetime_value_minor` | integer | `0` | `>= 0` |
| `prior_concession_count` | integer | `0` | `>= 0` |
| `prior_concessions_minor` | integer | `0` | `>= 0` |
| `customer_concession_headroom_minor` | integer | `0` | `>= 0` |
| `subscription_mrr_minor` | integer | `0` | `>= 0` |
| `budget_headroom_minor` | integer | `0` | `>= 0` |
| `purpose` | string \| null | `null` | `payment_failure_notice`, `payment_recovery_outreach`, `instrument_update_request`, `mandate_reauthorisation`, `step_up_authentication`, `promotional_winback` |
| `consent_state` | string | `never_granted` | `granted`, `withdrawn`, `expired`, `never_granted` |
| `authorisation_decision` | string | `denied` | `authorised`, `requires_step_up`, `denied` |
| `recovery_likelihood` | integer | `0` | `0–1000` |
| `churn_risk` | integer | `0` | `0–1000` |
| `merchant_review_first` | boolean | `true` | |

Ten further fields are **derived** and computed from the above: `is_money_movement`, `is_concession`,
`is_outreach`, `is_debit_retry`, `is_terminal_action`, `is_terminal_failure`, `has_prior_contact`,
`concession_percent_of_mrr`, `concession_exceeds_budget_headroom`,
`concession_exceeds_customer_ceiling`. Sending them is allowed — that is what lets a stored fact row
round-trip — but sending a value that contradicts the computation is a 422.

**Response**

| Field | Type | Notes |
| --- | --- | --- |
| `effect` | string | `allow`, `deny`, `require_approval`, `cap`. |
| `allowed` | boolean | True only for an unattended `allow`. Approval-required is not allowed yet. |
| `requires_approval` | boolean | |
| `denied` | boolean | |
| `matched_rule_name` | string \| null | The rule that produced the effect. Null when nothing matched. |
| `reason` | string | The matched rule's description, the joined approval reasons, or the no-match refusal. Gains a sentence when a cap bound the amount. |
| `proposed` | Amount | The amount as submitted. |
| `effective` | Amount | What the executor may use, after any cap. |
| `was_capped` | boolean | True only when a cap actually lowered the amount. |
| `capping_rule_name` | string \| null | The tightest matching cap rule — populated whenever a cap rule matched, even if `was_capped` is false because the proposal already sat under the ceiling. |
| `trace` | array&lt;RuleTraceView&gt; | Every rule considered, in order. |

**RuleTraceView**: `rule_name`, `priority`, `effect`, `matched` (boolean), `condition` (English),
`stopped_evaluation` (boolean — true on the DENY that ended the pass).

```bash
curl -s -X POST localhost:8000/api/policy/evaluate \
  -H 'content-type: application/json' \
  -d '{
        "action_type": "offer_winback_discount",
        "amount_minor": 20000,
        "subscription_mrr_minor": 149900,
        "budget_headroom_minor": 5000000,
        "customer_concession_headroom_minor": 74950,
        "consent_state": "granted",
        "purpose": "promotional_winback",
        "authorisation_decision": "authorised",
        "local_hour_ist": 11,
        "customer_tenure_days": 400,
        "hours_since_failure": 30,
        "recovery_likelihood": 600,
        "churn_risk": 400,
        "merchant_review_first": false
      }'
```

```json
{
  "effect": "allow",
  "allowed": true,
  "requires_approval": false,
  "denied": false,
  "matched_rule_name": "permit-outreach",
  "reason": "Contacting a consenting customer about a payment that failed is permitted.",
  "proposed": { "minor": 20000, "currency": "INR", "display": "₹200.00" },
  "effective": { "minor": 20000, "currency": "INR", "display": "₹200.00" },
  "was_capped": false,
  "capping_rule_name": "concession-proportionate-to-the-subscription",
  "trace": [
    {
      "rule_name": "consent-withdrawn-blocks-outreach",
      "priority": 10,
      "effect": "deny",
      "matched": false,
      "condition": "(is_outreach == true and consent_state in ['withdrawn', 'expired'])",
      "stopped_evaluation": false
    }
  ]
}
```

*(First trace entry shown; the trace carries all 27 rules.)*

**Errors**

| Status | When |
| --- | --- |
| `422` | An unknown key (`{"type": "extra_forbidden"}`), a value outside an enum, a number outside its range, or a derived field contradicting its components. |

---

### GET /api/ledger/demo

Builds a real posting sequence with the production posting functions and returns the validated
drafts. Nothing is written, and the balance check run here is the same one that runs before any
commit — a caller who can make this return `"balances": false` has found a genuine defect.

| Parameter | Type | Default | Constraint |
| --- | --- | --- | --- |
| `at_risk_minor` | integer | `149900` | `> 0` |
| `concession_minor` | integer | `20000` | `>= 0`. Zero omits the concession posting. |
| `recover` | boolean | `true` | `true` settles the remainder; `false` writes it off. |

The sequence is: `receivable_recognised`, then `concession_granted` when a concession is asked for,
then either `mandate_debit_settled` or `write_off` for the remainder.

**Response** — an array of transactions:

| Field | Type | Notes |
| --- | --- | --- |
| `txn_type` | string | `receivable_recognised`, `concession_granted`, `mandate_debit_settled`, `write_off`. |
| `narration` | string | Human-readable, with the amount formatted. |
| `idempotency_key` | string | Caller-generated, stable across retries of the same posting. |
| `balances` | boolean | Debits equal credits. |
| `total_debits` | Amount | |
| `total_credits` | Amount | |
| `entries` | array&lt;LedgerEntryView&gt; | `account` (label), `direction` (`debit`/`credit`), `amount` (Amount). |

```bash
curl -s 'localhost:8000/api/ledger/demo?at_risk_minor=149900&concession_minor=20000&recover=true'
```

```json
[
  {
    "txn_type": "receivable_recognised",
    "narration": "Receivable recognised, ₹1,499.00 at risk",
    "idempotency_key": "anvil_8d3838af9b854a5363b919bbe18a3c0c",
    "balances": true,
    "total_debits": { "minor": 149900, "currency": "INR", "display": "₹1,499.00" },
    "total_credits": { "minor": 149900, "currency": "INR", "display": "₹1,499.00" },
    "entries": [
      { "account": "customer:receivable/cus_demo", "direction": "debit",  "amount": { "minor": 149900, "currency": "INR", "display": "₹1,499.00" } },
      { "account": "merchant:revenue",             "direction": "credit", "amount": { "minor": 149900, "currency": "INR", "display": "₹1,499.00" } }
    ]
  }
]
```

*(First transaction shown; a concession posting has four legs and the sequence returns three
transactions with these defaults.)*

**Errors**

| Status | When |
| --- | --- |
| `422` | `concession_minor >= at_risk_minor` — `{"detail": "a concession cannot be larger than the amount at risk"}`. Also the usual validation failures on the query types and bounds. |

---

## Operations

### GET /api/cases

The at-risk book. Live queue cases come first, because those carry real graph state.

| Parameter | Type | Default | Constraint |
| --- | --- | --- | --- |
| `limit` | integer | `60` | `1–500`. Applied to the combined list, live cases included. |
| `unmapped_only` | boolean | `false` | Filters the non-live remainder to cases whose reason code no table recognises. **Live queue cases are always listed regardless**, so a filtered response still opens with them. |

With the default seed the world holds 171 open cases, eight of which are live.

**CaseSummary**

| Field | Type | Notes |
| --- | --- | --- |
| `case_id` | string | Prefixed ULID, `cse_…`. |
| `customer_name` | string | |
| `customer_id` | string | `cus_…`. |
| `arm` | string | `control`, `baseline`, `anvil`. |
| `status` | string | The graph's status for a live case (`planning`, `recovered`, …); always `open` for the rest. |
| `at_risk` | Amount | |
| `recovered` | Amount | Zero unless a live case has recovered money. |
| `failure_class` | string \| null | The simulator's ground truth. |
| `observed_failure_class` | string \| null | What the graph diagnosed. Null for non-live cases. |
| `raw_code` | string | The issuer's code or narration as received. |
| `code_is_unmapped` | boolean | True when no table recognises the code. |
| `classified_deterministically` | boolean \| null | Null for non-live cases. |
| `attempts` | integer | |
| `contacts` | integer | |
| `bank` | string | |
| `mandate_type` | string | `upi_autopay`, `enach`, `card_mandate`, `reserve_pay`, `delegated_agent`. |
| `recovered_flag` | boolean | `recovered.minor > 0`. |

```bash
curl -s 'localhost:8000/api/cases?limit=2'
```

```json
[
  {
    "case_id": "cse_5VYY8FBPCGFGQR4RAF0748BEM2",
    "customer_name": "Neha Gupta",
    "customer_id": "cus_3WP5XS33QZVHMNGJP5NDF65SC2",
    "arm": "anvil",
    "status": "planning",
    "at_risk": { "minor": 9900, "currency": "INR", "display": "₹99.00" },
    "recovered": { "minor": 0, "currency": "INR", "display": "₹0.00" },
    "failure_class": "insufficient_funds",
    "observed_failure_class": "insufficient_funds",
    "raw_code": "Z9",
    "code_is_unmapped": false,
    "classified_deterministically": true,
    "attempts": 0,
    "contacts": 0,
    "bank": "Harbour National",
    "mandate_type": "upi_autopay",
    "recovered_flag": false
  }
]
```

**Errors** — `422` for `limit` outside `1–500`.

---

### GET /api/cases/{case_id}

One case, with its full timeline and every action's legitimacy trail.

| Parameter | Type | Notes |
| --- | --- | --- |
| `case_id` | path, string | A `cse_…` id from `GET /api/cases`. |

**CaseDetail** — every `CaseSummary` field, plus:

| Field | Type | Notes |
| --- | --- | --- |
| `narration` | string | The bank's narration. |
| `failed_at` | string | Pre-formatted IST, e.g. `Wed 02 Sep 2026, 00:30 IST`. |
| `mandate_reference` | string | The mandate's external reference (UMN or equivalent). |
| `mandate_max` | Amount | The mandate's per-debit ceiling. |
| `tenure_days` | integer | |
| `language` | string | ISO code, e.g. `hi`, `en`. |
| `timeline` | array&lt;TimelineItem&gt; | `node`, `summary`, `at` (ISO-8601). **Empty for a case that is not in the live queue.** |
| `actions` | array&lt;ActionView&gt; | Also empty for a non-live case. |
| `ledger` | array&lt;LedgerTransactionView&gt; | The same draft postings `/api/ledger/demo` returns, built from this case's amounts. Empty for a non-live case. |
| `degraded` | boolean | True when the graph ran on the deterministic fallback. |
| `degraded_reason` | string \| null | |
| `model_safety_events` | integer | Out-of-bounds proposals refused for this case. |

**ActionView**: `action_id`, `action_type`, `status` (`proposed`, …), `amount` (Amount \| null),
`rationale` (string \| null), `authorisation_decision` (string \| null), `policy_effect`
(string \| null), `scheduled_for` (string \| null).

```bash
curl -s localhost:8000/api/cases/cse_5VYY8FBPCGFGQR4RAF0748BEM2
```

```json
{
  "case_id": "cse_5VYY8FBPCGFGQR4RAF0748BEM2",
  "status": "planning",
  "narration": "Harbour National: insufficient funds [Z9]",
  "failed_at": "Wed 02 Sep 2026, 00:30 IST",
  "mandate_reference": "UMN12TF36FJ9X6HM5CE",
  "mandate_max": { "minor": 13900, "currency": "INR", "display": "₹139.00" },
  "tenure_days": 22,
  "language": "hi",
  "timeline": [
    { "node": "ingest",   "summary": "Case opened; 99.00 at risk", "at": "2026-09-01T19:00:00+00:00" },
    { "node": "classify", "summary": "insufficient_funds resolved from the code tables (z9)", "at": "2026-09-01T19:00:00+00:00" },
    { "node": "score",    "summary": "recovery 510/1000, churn 276/1000, priority 55/1000", "at": "2026-09-01T19:00:00+00:00" }
  ],
  "actions": [
    {
      "action_id": "act_01M1JTH9CPK5EP3XPXTSHBP0VN",
      "action_type": "retry_debit",
      "status": "proposed",
      "amount": { "minor": 9900, "currency": "INR", "display": "₹99.00" },
      "rationale": "Deterministic fallback: insufficient_funds is retryable and the scheduler has an hour for it. …",
      "authorisation_decision": "authorised",
      "policy_effect": "require_approval",
      "scheduled_for": null
    }
  ],
  "ledger": [],
  "degraded": true,
  "degraded_reason": "deterministic fallback: the planner model was unavailable",
  "model_safety_events": 0
}
```

*(Summary fields and the ledger array trimmed.)*

**Errors**

| Status | When |
| --- | --- |
| `404` | No such case — `{"detail": "no case cse_nope"}`. |

**Known defect.** For a case that is *not* in the live queue and whose reason code *is* mapped, the
summary half of the response (`case_id`, `customer_name`, `at_risk`, …) is taken from the first
approval-queue item rather than from the requested case, while the detail-only fields above are the
requested case's. The internal lookup passes FastAPI's `Query` default object as `unmapped_only`,
which is truthy, so mapped cases are filtered out of the list it searches. Live cases and unmapped
non-live cases are unaffected.

---

### GET /api/approvals

No parameters. Everything waiting on a person. Each item is a graph paused on a committed
checkpoint; resolved items disappear from the list.

The queue is filled at startup with up to 8 non-control-arm cases
(`LIVE_QUEUE_SIZE` in [`anvil/api/state.py`](../../anvil/api/state.py)).

**ApprovalItem**

| Field | Type | Notes |
| --- | --- | --- |
| `approval_id` | string | `apr_` plus the last ten characters of the case id — derived from the case, so two pauses can never collide onto one entry. |
| `case_id` | string | |
| `customer_name` | string | |
| `action_type` | string | From the interrupt payload; falls back to its `kind`, then to `"action"`. |
| `amount` | Amount \| null | Null for an action that moves no money. |
| `rationale` | string \| null | Why the planner proposed it. |
| `escalation_reason` | string | The interrupt's reason, or a default sentence when it carries none. |
| `requested_at` | string | Pre-formatted IST, e.g. `02 Sep 00:30 IST`. |
| `at_risk` | Amount | |
| `failure_class` | string \| null | As diagnosed by the graph. |
| `recovery_likelihood` | integer \| null | 0–1000. |
| `churn_risk` | integer \| null | 0–1000. |
| `version` | integer | Starts at 1 and increments on every accepted decision. Echo it back when deciding. |

```bash
curl -s localhost:8000/api/approvals
```

```json
[
  {
    "approval_id": "apr_AF0748BEM2",
    "case_id": "cse_5VYY8FBPCGFGQR4RAF0748BEM2",
    "customer_name": "Neha Gupta",
    "action_type": "retry_debit",
    "amount": { "minor": 9900, "currency": "INR", "display": "₹99.00" },
    "rationale": "Deterministic fallback: insufficient_funds is retryable and the scheduler has an hour for it. …",
    "escalation_reason": "policy required a human decision before this action executes",
    "requested_at": "02 Sep 00:30 IST",
    "at_risk": { "minor": 9900, "currency": "INR", "display": "₹99.00" },
    "failure_class": "insufficient_funds",
    "recovery_likelihood": 510,
    "churn_risk": 276,
    "version": 1
  }
]
```

Errors: none.

---

### POST /api/approvals/{approval_id}

Resumes the paused graph with a human decision. The graph then runs authorisation, policy and the
executor for real, and the response carries the resulting case state.

| Parameter | Type | Notes |
| --- | --- | --- |
| `approval_id` | path, string | From `GET /api/approvals`. |

**Body**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `decision` | string | yes | Must match `^(approve\|reject\|edit)$`. |
| `decided_by` | string | yes | The named human. Recorded in the timeline. |
| `note` | string \| null | no | Free text; omitted from the resume payload when empty. |
| `edited_amount_minor` | integer \| null | no | `> 0`. Applied only when `decision` is `edit`. |
| `version` | integer | yes | The version the operator was shown. A mismatch is a 409. |

For an `afa_step_up` interrupt the resume also carries `succeeded = decision != "reject"`, so
rejecting a step-up models a failed authentication rather than a refused action.

**Response**

| Field | Type | Notes |
| --- | --- | --- |
| `approval_id` | string | Echoes the path parameter. |
| `outcome` | string | Echoes the decision. |
| `case_status` | string | The graph's status after resuming — e.g. `recovered`, or `planning` if the case paused again. |
| `recovered` | Amount | Money recovered on this case so far. |
| `timeline` | array&lt;TimelineItem&gt; | The case's full history, now including the `approval` node. |

```bash
curl -s -X POST localhost:8000/api/approvals/apr_XVKGBYA3DT \
  -H 'content-type: application/json' \
  -d '{"decision":"approve","decided_by":"ops@anvil.test","note":"payday window looks right","version":1}'
```

```json
{
  "approval_id": "apr_XVKGBYA3DT",
  "outcome": "approve",
  "case_status": "recovered",
  "recovered": { "minor": 26730, "currency": "INR", "display": "₹267.30" },
  "timeline": [
    { "node": "policy",   "summary": "retry_debit require_approval: This merchant is in review-first mode, so every action is drafted for a human rather than executed.", "at": "2026-09-02T05:00:00+00:00" },
    { "node": "approval", "summary": "retry_debit approve by ops@anvil.test", "at": "2026-09-02T05:00:00+00:00" },
    { "node": "schedule", "summary": "Attempt 1 scheduled now, at Wed 02 Sep 10:30 IST, at 78.4% expected success, because 10:00 IST is clear of the overnight issuer maintenance window.", "at": "2026-09-02T05:00:00+00:00" },
    { "node": "execute",  "summary": "recovered 267.30", "at": "2026-09-02T05:00:00+00:00" }
  ]
}
```

*(Timeline trimmed to the nodes the decision produced.)*

A case can pause more than once. When it does it stays in the queue, `version` increments, and the
next decision must send the new version — a `reject` that sends the graph back to planning typically
returns `case_status: "planning"` and leaves the item pending at version 2.

**Errors**

| Status | When | Body |
| --- | --- | --- |
| `404` | No pending approval with that id. | `{"detail": "no pending approval apr_nope"}` |
| `409` | The item was already resolved. | `{"detail": "this action has already been resolved by someone else"}` |
| `409` | Version mismatch — somebody else acted first. | `{"detail": "you were shown version 7 but the current version is 1; somebody else acted first"}` |
| `422` | `decision` outside the pattern, `edited_amount_minor <= 0`, or a missing required field. | FastAPI validation detail |

---

## Evidence

### GET /api/batch

Runs the seeded three-arm experiment and returns the evidence report — the same machinery as
`make batch`, served as JSON.

| Parameter | Type | Default | Constraint |
| --- | --- | --- | --- |
| `seed` | integer | `20260902` | `> 0` |
| `size` | integer | `2000` | `100–4000` (population size, not case count) |
| `with_model` | boolean | `false` | Model the LLM classifier as available, so its contribution can be measured rather than asserted. |

Results are cached per `(seed, size, with_model)` and the batch runs on a worker thread. The default
size takes a few seconds on a first call and returns from cache thereafter. Arms are split evenly
(`EVEN_SPLIT`), not 10% holdout, because narrow-enough intervals matter more here than a realistic
holdout.

**BatchView**

| Field | Type | Notes |
| --- | --- | --- |
| `seed` | integer | |
| `population_size` | integer | Echoes `size`. |
| `case_count` | integer | Cases the population actually opened. |
| `total_at_risk` | Amount | |
| `model_available` | boolean | Echoes `with_model`. |
| `arms` | array&lt;ArmView&gt; | `control`, `baseline`, `anvil`, in that order. |
| `comparisons` | array&lt;ComparisonView&gt; | Pairwise, on the *difference*. |
| `unmapped_codes` | integer | Cases whose code no table recognised. |
| `classified_deterministically` | integer | |
| `classified_by_model` | integer | Cases the classifier attributed to the model rather than the tables. |
| `model_safety_events` | integer | Out-of-bounds proposals refused. |
| `calibration_verdict` | string | States plainly when there is too little data to assess calibration. |
| `calibration_buckets` | array&lt;CalibrationBucketView&gt; | |
| `limitations` | array&lt;string&gt; | What this run does and does not prove, generated from the run itself. |

**ArmView**: `arm`, `label` (e.g. `anvil (the agent)`), `cases`, `recovered_count`, `rate_bps`,
`rate_ci_low_bps`, `rate_ci_high_bps`, `at_risk`, `recovered`, `net_recovered`, `total_cost`,
`attempts`, `contacts`, `by_failure_class` — an object mapping failure class to a two-element array
`[cases, recovered]`.

**ComparisonView**: `treatment`, `against`, `difference_bps`, `ci_low_bps`, `ci_high_bps`,
`significant` (boolean), `underpowered` (boolean), `minimum_detectable_bps`, `z_score` (float, three
decimals), `net_difference` (Amount), `verdict` — the result stated in words, including when it is
not significant.

**CalibrationBucketView**: `label` (e.g. `20-30%`), `count`, `predicted_bps`, `observed_bps`,
`gap_bps`.

```bash
curl -s 'localhost:8000/api/batch?seed=20260902&size=2000'
```

```json
{
  "seed": 20260902,
  "population_size": 2000,
  "case_count": 394,
  "total_at_risk": { "minor": 6635475, "currency": "INR", "display": "₹66,354.75" },
  "model_available": false,
  "arms": [
    {
      "arm": "control",
      "label": "control (no intervention)",
      "cases": 129,
      "recovered_count": 28,
      "rate_bps": 2171,
      "rate_ci_low_bps": 1473,
      "rate_ci_high_bps": 2868,
      "at_risk": { "minor": 2236410, "currency": "INR", "display": "₹22,364.10" },
      "recovered": { "minor": 420255, "currency": "INR", "display": "₹4,202.55" },
      "net_recovered": { "minor": 420255, "currency": "INR", "display": "₹4,202.55" },
      "total_cost": { "minor": 0, "currency": "INR", "display": "₹0.00" },
      "attempts": 0,
      "contacts": 0,
      "by_failure_class": {
        "insufficient_funds": [67, 13],
        "limit_exceeded": [8, 2],
        "issuer_technical": [47, 13],
        "unknown": [2, 0],
        "risk_declined": [5, 0]
      }
    }
  ],
  "comparisons": [
    {
      "treatment": "anvil",
      "against": "baseline",
      "difference_bps": -2170,
      "ci_low_bps": -3162,
      "ci_high_bps": -1181,
      "significant": true,
      "underpowered": false,
      "minimum_detectable_bps": 1207,
      "z_score": -4.084,
      "net_difference": { "minor": -572830, "currency": "INR", "display": "-₹5,728.30" },
      "verdict": "Significantly WORSE than baseline: the 95% interval excludes zero."
    }
  ],
  "unmapped_codes": 103,
  "classified_deterministically": 100,
  "classified_by_model": 31,
  "model_safety_events": 0,
  "calibration_verdict": "Systematically over-confident by 8.8 points across 398 attempts (expected calibration error 11.9%). The retry curves need re-fitting against observed outcomes.",
  "calibration_buckets": [
    { "label": "10-20%", "count": 10, "predicted_bps": 1572, "observed_bps": 3000, "gap_bps": -1428 }
  ],
  "limitations": [
    "The language model was unavailable throughout, so every case ran on the deterministic fallback. …"
  ]
}
```

*(One of three arms, the third of three comparisons, one calibration bucket and one of six
limitations shown. The endpoint reports an unfavourable verdict when the run produces one — the
`verdict` and `limitations` strings are generated from the outcomes, not written by hand.)*

**Errors** — `422` for `seed <= 0` or `size` outside `100–4000`.

---

## Errors

### Two shapes

Most failures come back in FastAPI's standard shape. A raised `HTTPException` gives a string:

```json
{ "detail": "no pending approval apr_nope" }
```

A request-validation failure gives the list, naming the field and the rule it broke:

```json
{
  "detail": [
    { "type": "extra_forbidden", "loc": ["body", "nonsense"], "msg": "Extra inputs are not permitted", "input": 1 }
  ]
}
```

The application also registers a handler for `AnvilError`
([`anvil/core/errors.py`](../../anvil/core/errors.py)), which renders the domain taxonomy directly:

```json
{
  "error": {
    "code": "policy_denied",
    "message": "no policy rule matched this action, and Anvil denies what no rule permits. Add a rule covering it if it should be allowed.",
    "retryable": false,
    "context": {
      "bundle_id": "pol_default",
      "bundle_version": 1,
      "rule": null,
      "action_type": "offer_winback_discount"
    }
  }
}
```

The four keys are fixed — `code`, `message`, `retryable`, `context` — and `context` carries whatever
the raise site attached (the example shows what `PolicyDecision.raise_if_denied` attaches).

The status comes from the error class, and `code` is stable — a client branches on it rather than
parsing prose. The routers translate the two errors they raise themselves (`NotFound`,
`OptimisticLockConflict`) into `HTTPException`, so those arrive in the `detail` shape; the envelope is
what you get when an `AnvilError` propagates out of the code an endpoint calls — for example from a
graph resumed by an approval decision.

### The taxonomy

| Code | Status | Retryable | Meaning |
| --- | --- | ---: | --- |
| `invariant_violation` | 500 | no | A financial invariant was broken. Never handled; aborts and pages a human. |
| `unbalanced_transaction` | 500 | no | Debits did not equal credits. |
| `ledger_immutability_violation` | 500 | no | Something tried to edit a posted entry. |
| `domain_error` | 422 | no | The system is right and is saying no. |
| `authorisation_denied` | 403 | no | No valid authorisation for the action. |
| `step_up_required` | 401 | no | The customer must re-authenticate. |
| `policy_denied` | 403 | no | A policy rule refused it. |
| `budget_exhausted` | 409 | no | No concession headroom. |
| `consent_missing` | 403 | no | No consent for that specific purpose. |
| `stopping_rule_fired` | 409 | no | A deterministic stopping rule ended the case. |
| `insufficient_reservation` | 409 | no | The budget reservation could not cover the draw. |
| `conflict` | 409 | no | Generic conflict. |
| `optimistic_lock_conflict` | 409 | no | Two operators resolved the same approval. |
| `duplicate_event` | 200 | no | An already-processed webhook. Answered 200, no business logic re-run. |
| `stale_event` | 200 | no | An out-of-order webhook older than held state. |
| `external_error` | 502 | **yes** | A boundary failed. |
| `gateway_error` | 502 | **yes** | |
| `gateway_timeout` | 504 | no | The outcome is genuinely unknown. Reconcile, never blind-retry. |
| `webhook_verification_failed` | 400 | no | Signature check failed. |
| `webhook_replay_rejected` | 400 | no | Outside the replay window. |
| `llm_error` / `llm_timeout` / `llm_rate_limited` | 502 | **yes** | |
| `structured_output_invalid` | 502 | **yes** | The model returned something the schema rejects. |
| `model_proposed_out_of_bounds` | 422 | no | Refused, and counted as a model-safety event. |
| `fixture_missing` | 500 | no | Offline mode has no recorded response for a call. |
| `not_found` | 404 | no | |
| `validation_error` | 400 | no | |

Most of these belong to code paths the console endpoints do not reach; they are listed because the
`code` values are the stable contract for any client that does.

### Status codes in practice

| Status | Where it comes from |
| --- | --- |
| `200` | Every successful request, including `POST`s. Nothing here returns 201. |
| `404` | Unknown case, unknown approval, unknown route. |
| `409` | An approval already resolved, or a version mismatch. |
| `422` | Request validation, and the ledger demo's concession check. |
| `500` | An unhandled exception. The taxonomy's invariant violations land here. |

---

## The console

`GET /` serves [`anvil/api/static/index.html`](../../anvil/api/static/index.html) — one self-contained
page, no build step, no CORS, no second process. `/static` is mounted at the same directory. Both
routes exist only if that directory is present; the application starts without it and serves the API
alone.

The page is a client of the endpoints above and calls nothing else. Its seven views map onto them
directly: **Approval inbox** (`/api/approvals`, `POST /api/approvals/{id}`), **Recovery cockpit**
(`/api/batch`), **Cases** (`/api/cases`, `/api/cases/{id}`), **Retry scheduler**
(`/api/scheduler/explain`), **Policy engine** (`/api/policy/bundle`, `/api/policy/evaluate`),
**Ledger** (`/api/ledger/demo`) and **Classifier** (`/api/classify`). Its design system is documented
in [DESIGN.md](design-system.md).

---

## Rough edges worth knowing

- **State is per-process and per-restart.** Approvals resolved in one run are pending again in the
  next; there is no persistence behind this API.
- **The queue is deliberately small.** Eight items, filled at startup from non-control-arm cases. A
  case that finishes without pausing never enters it, and a case whose graph raises during startup is
  skipped so one bad case cannot empty the queue.
- **`GET /api/cases/{case_id}` mis-attributes the summary block** for mapped, non-live cases — see the
  defect note under that endpoint.
- **`/api/batch` caches per `(seed, size, with_model)`** for the life of the process, so a repeated
  call cannot be reloaded until it gives a nicer number.
- **No auth, no CORS, no rate limiting**, and the approval endpoint mutates state.
