# Anvil — Architecture

**Razorpay AI Buildathon 2026 · Track 03: AI Revenue Recovery**

> Vulcan is the forge. Anvil is where at-risk revenue gets hammered back into settled revenue.

---

## 1. The problem, stated precisely

A subscription business with ₹1 crore of monthly recurring revenue on UPI Autopay and e-NACH
mandates will see **6–12% of debit attempts fail** in any given month. Roughly two thirds of those
failures are *recoverable* — insufficient funds that clear on payday, an expired card that the
customer would happily update, a bank-side technical decline that succeeds on retry four hours
later. The other third are *terminal* — a revoked mandate, a closed account, a customer who has
already decided to leave.

The money is not lost at the moment of the decline. It is lost in the **48 hours afterwards**,
during which most merchants do exactly one of two wrong things:

1. **Retry blindly on a fixed schedule** (day 1, day 3, day 5). This burns the mandate's retry
   allowance on decline codes that were never going to clear, and misses the ones that would have
   cleared if retried at the right hour.
2. **Escalate identically to everyone.** The customer whose card expired gets the same dunning
   email as the customer who deliberately revoked their mandate. One is insulted; the other is
   ignored.

Both failures are *decision* failures, not *infrastructure* failures. That is what makes this an
agent problem — and precisely what makes the boundary between the agent and the ledger the most
important line in the system.

## 2. Thesis

> **The model decides. The ledger disposes. Nothing the model says can move money.**

Anvil is built on a hard separation between a *stochastic* decision layer and a *deterministic*
execution layer. The LLM proposes; a deterministic policy engine, a cryptographically-modelled
mandate registry, and an append-only double-entry ledger dispose. Every rupee that moves is
traceable to (a) a valid authorisation object, (b) a policy evaluation that permitted it, and
(c) either an autonomous decision inside pre-agreed bounds or a named human's approval.

This inverts the usual agent architecture. Most agent frameworks put the model in the driver's
seat and bolt guardrails on afterwards. Anvil puts the *invariants* in the driver's seat and gives
the model a bounded steering wheel.

## 3. Where AI is used — and deliberately is not

Judges for this track are explicitly asked to penalise LLMs bolted onto problems that deterministic
logic already solves. So this section is stated first, not last.

### AI is used for four things, because rules genuinely fail at them

| # | Job | Why a rule engine loses |
|---|-----|-------------------------|
| 1 | **Failure diagnosis.** Map a heterogeneous signal bundle — gateway error string, bank narration, issuer reason code, the customer's payment history, prior support tickets, mandate metadata — onto a structured `RecoveryHypothesis`. | The inputs are unstructured free text from dozens of banks with no shared vocabulary. `"INSUFFICIENT_BALANCE"`, `"Insufficient Funds"`, `"NPCI:U30 debit failed"` and `"A/c bal low"` are the same fact. Enumerating that mapping is a losing game; generalising over it is exactly what a language model is for. |
| 2 | **Recovery planning.** Choose a sequenced plan from a *closed* action space, under a live budget, given the diagnosis and the customer's value and history. | The action space is closed, but the trade-off surface is not: whether a ₹200 concession is worth spending to protect a ₹1,499/mo mandate depends on churn risk, tenure, prior concessions, and how the customer responded last time. This is judgement under a constraint set. |
| 3 | **Customer communication.** Generate the outreach copy, in the customer's language and register, matched to the diagnosed cause. | "Your card ending 4242 expired" and "your account was short by ₹340 on Tuesday" require materially different tone. Templating this across languages and causes produces the exact insulting mismatch described in §1. |
| 4 | **Policy compilation.** Translate a merchant's plain-English rules into a versioned, diffable, deterministic policy artifact. | Natural language is the merchant's native format; a rule DSL is not. The model authors the policy — it never *is* the policy. The compiled artifact is reviewed and approved before activation, and from then on it executes deterministically with no model in the loop. |

### AI is deliberately NOT used for these, and each was considered

| Job | What does it instead | Why |
|-----|---------------------|-----|
| **Retry timing** | A deterministic scheduler driven by a calibrated per-decline-code retry curve (§7), plus salary-cycle and bank-maintenance-window awareness. | This is a well-posed statistical estimation problem with abundant labelled data. An LLM would be strictly worse *and* non-reproducible. Asking a model "when should I retry?" is the canonical example of the mistake this track penalises. |
| **Authorisation** | The mandate registry (§8). Cryptographic/structural check against a stored authorisation object. | An authorisation decision must be *provable*, not *plausible*. There is no acceptable false-positive rate. |
| **Budget arithmetic** | Deterministic ledger reservations under `SELECT … FOR UPDATE`. | Models cannot be trusted with arithmetic that must balance to the paisa, and the ledger must remain correct even when every model call fails. |
| **Stopping rules** | Deterministic policy predicates (attempt caps, contact-frequency caps, quiet hours, consent state). | A stopping rule that a model can talk itself out of is not a stopping rule. |
| **Money movement** | Idempotent gateway calls behind the ledger. | The model never holds a credential and never calls a payment API. It emits a *proposal*; the executor validates and performs it. |

## 4. System shape

Two processes, one Postgres, one schema.

```
                    ┌──────────────────────────────────────────────┐
   Razorpay  ──────▶│  api  (FastAPI, ASGI)                        │
   webhooks         │   · signature verify → replay window → dedupe│
                    │   · console REST + SSE                       │
   Console   ──────▶│   · HITL approve / reject / edit             │
                    └───────────────┬──────────────────────────────┘
                                    │  same transaction
                    ┌───────────────▼──────────────────────────────┐
                    │  Postgres                                     │
                    │   · ledger_entries        (append-only)       │
                    │   · domain_events         (append-only)       │
                    │   · outbox                (transactional)     │
                    │   · langgraph checkpoints (durable state)     │
                    │   · read models           (derived)           │
                    └───────────────┬──────────────────────────────┘
                                    │  outbox relay
                    ┌───────────────▼──────────────────────────────┐
                    │  worker  (LangGraph executor)                 │
                    │   · recovery graph per case                   │
                    │   · interrupt() → HITL → resume               │
                    │   · channel dispatch, gateway calls           │
                    └──────────────────────────────────────────────┘
```

**Why this shape.** A runaway model call cannot starve webhook ingestion, and workers scale
independently — the benefit of a control/data-plane split, for the cost of one extra process rather
than five extra services. Because the event log and the read model commit in the *same* Postgres
transaction, we get a provably complete audit trail and free time-travel replay without paying for
eventual consistency in the UI. The module boundaries are already the service boundaries and the
event log is already the integration contract: splitting this into services later is a deployment
change, not a rewrite.

## 5. Module map

| Module | Owns | Must never |
|--------|------|-----------|
| `anvil/domain` | Pure value objects, enums, the decline taxonomy. No I/O. | Import any other `anvil` module. |
| `anvil/core` | Config, structured logging, errors, ID generation, injectable clock. | Contain business logic. |
| `anvil/db` | Engine, session, ORM models, migrations, the outbox. | Contain business logic. |
| `anvil/ledger` | Append-only double-entry ledger, balances, reservations. | Expose an UPDATE path to any balance. |
| `anvil/mandates` | Authorisation registry, debit-capability checks, AFA step-up. | Approve anything not backed by a stored authorisation. |
| `anvil/policy` | Compiled policy artifacts, the evaluator, the NL compiler. | Let a model evaluate a policy at decision time. |
| `anvil/risk` | Decline classification, retry curves, at-risk detection, churn/value scoring. | Call an LLM for retry timing. |
| `anvil/graph` | The LangGraph recovery graph, typed state, nodes, interrupts. | Move money directly; it emits proposals. |
| `anvil/channels` | Outreach adapters, the outbox, consent + frequency enforcement. | Send without a consent check and a policy pass. |
| `anvil/gateway` | Razorpay client, webhook verification, idempotency keys. | Retry without an idempotency key. |
| `anvil/llm` | Claude client, structured output, PII redaction, deterministic fixtures. | Return unvalidated model output to a caller. |
| `anvil/simulator` | Issuer, customer and world simulation; seeded and reproducible. | Be reachable in live mode. |
| `anvil/evidence` | Experiment assignment, treatment/holdout arms, lift statistics. | Compute lift without a control arm. |
| `anvil/audit` | Immutable audit log, redaction, replay/time-travel. | Store raw PII. |
| `anvil/api` | HTTP translation only. | Contain business logic. |

## 6. The invariants

These are enforced by tests that fail the build. They are the spine of the whole submission.

1. **No balance is ever mutated.** Balances are derived by summing append-only `ledger_entries`.
   There is no `UPDATE` statement against a balance anywhere in the codebase, enforced by a test
   that greps the ORM layer and by a Postgres rule denying UPDATE/DELETE on `ledger_entries`.
2. **Every ledger transaction balances to zero.** Sum of debits equals sum of credits, per
   transaction, per currency, checked in the same transaction that writes it.
3. **Money is integer minor units.** `Money` is `(int paise, Currency)`. Floats are banned from the
   money path; a test asserts no `float` appears in any ledger or gateway signature.
4. **Every inbound webhook is processed at most once.** Unique constraint on the Razorpay event id;
   a duplicate returns `200 OK` without re-running business logic.
5. **Every outbound money-moving call carries a caller-generated idempotency key** that is stable
   across retries of the same logical action.
6. **No action executes without a valid authorisation.** Every execution checks the mandate registry
   and fails *closed*.
7. **No action executes without a policy pass.** The evaluation result — decision, matched rule,
   version — is persisted with the action.
8. **Concessions draw against a reserved budget.** Reservation happens under `SELECT … FOR UPDATE`
   before the action, so concurrent cases cannot jointly overspend a merchant's budget.
9. **Every state transition is replayable.** The graph state at each checkpoint plus the event log
   reconstruct any case at any point in its life.
10. **The audit log contains no raw PII.** Redaction happens before persistence, not on read.

## 7. Decline taxonomy and retry science

Failures are classified into a fixed set of `FailureClass` values, each with an associated
recovery posture. Classification is a two-stage process: a deterministic lookup against known
issuer/NPCI reason codes handles the ~80% of cases with a recognised code, and the LLM classifier
handles the unrecognised remainder — with its output constrained to the same closed enum.

| Class | Example raw codes | Posture | Retryable |
|-------|-------------------|---------|-----------|
| `INSUFFICIENT_FUNDS` | `U30`, `INSUFFICIENT_BALANCE`, `Z9`, "A/c bal low" | Retry aligned to salary cycle; escalate gently. | Yes, high value |
| `INSTRUMENT_EXPIRED` | `CARD_EXPIRED`, `54` | Retry is pointless. Request instrument update. | No |
| `ISSUER_TECHNICAL` | `91`, `BANK_DOWN`, `U69` | Fast retry outside the maintenance window. | Yes, highest value |
| `LIMIT_EXCEEDED` | `61`, `65`, per-txn cap breaches | Retry after limit reset; consider split payment. | Yes |
| `MANDATE_REVOKED` | `UMN_NOT_FOUND`, `MANDATE_CANCELLED` | Terminal for debit. Re-authorisation journey only. | No |
| `MANDATE_PAUSED` | `MANDATE_ON_HOLD` | Wait for resume, or prompt the customer. | Deferred |
| `ACCOUNT_CLOSED` | `RC 02`, `ACCOUNT_BLOCKED` | Terminal. Instrument change or churn. | No |
| `RISK_DECLINED` | issuer fraud rules, velocity blocks | Do not retry — retrying worsens the issuer score. | No |
| `AUTH_REQUIRED` | AFA / step-up required | Trigger step-up authentication. | After step-up |
| `UNKNOWN` | anything unmapped | Single conservative retry, then human review. | Once |

Each class carries a **retry curve**: a discrete hazard function giving `P(success | class, attempt
n, hours since failure, hour-of-day, day-of-month)`. In offline mode these curves are seeded from
the simulator's ground-truth generative parameters, which lets us prove the scheduler is
recovering real signal rather than memorising noise. The scheduler picks the attempt time that
maximises expected recovered value net of the cost of consuming a retry allowance — a plain
argmax over a tabulated function. **No model is involved.**

## 8. Authorisation as a precondition, not a policy

Every recovery action must present a valid authorisation before it executes. This is what turns
"bounded" from a convention into a precondition.

**A note on what is and is not live.** UPI Autopay and e-NACH mandates are production rails today.
UPI Circle and Reserve Pay exist, but NPCI's **Unified Agent Protocol -- the framework that would let a
verified AI agent transact on them -- has not launched**: it is expected to be unveiled at Global
Fintech Fest 2026 and still requires RBI approval. Anvil therefore does not integrate with UAP and does
not claim to. It models delegated agent authority and Single Block Multi Debit blocks as first-class
authorisation objects *in the shape UAP describes*, so that the day the protocol lands the registry
gains an issuer rather than a redesign. Every statement about UAP in this repository should be read as
describing a proposed standard.

Modelled authorisation types:

- **`UPI_AUTOPAY` / `ENACH` mandate** — a stored mandate with a UMN, maximum debit amount,
  frequency, validity window, and a consumed-attempts counter.
- **`RESERVE_PAY` block** — Single Block Multi Debit. A pre-authorised amount is *blocked*; the
  agent may debit against the block repeatedly without a fresh PIN, up to the blocked total.
  Represented in the ledger as a real reservation, so a partially-consumed block cannot be
  double-spent.
- **`DELEGATED_AGENT` authority** — modelled on UPI Circle: a principal delegates to a named agent
  with a per-transaction cap, a per-period cap, and an expiry.

`authorise(action) → Authorised | RequiresStepUp | Denied`. The check is structural and total: an
action whose amount, frequency, counterparty, or window falls outside every held authorisation
returns `Denied`. An action that is *within a principal's* limits but exceeds the *agent's*
delegated cap returns `RequiresStepUp` — which interrupts the graph, dispatches an AFA challenge
to the customer, and resumes only on cryptographic confirmation. Failing closed is the default at
every branch.

## 9. Policy engine

A `PolicyBundle` is a versioned, immutable, content-addressed artifact: an ordered list of rules
over a typed fact set, each with an effect (`ALLOW`, `DENY`, `REQUIRE_APPROVAL`, `CAP`). Evaluation
is pure, total, and side-effect free — the same facts always produce the same decision, and the
decision records which rule fired.

The NL compiler is a *build-time* tool, not a runtime path: merchant prose → proposed bundle →
human-readable diff against the active bundle → explicit approval → activation. A bundle that has
not been approved cannot be loaded by the evaluator.

Default bundle ships with contact-frequency caps, quiet hours, per-customer and per-merchant
concession ceilings, attempt caps per mandate cycle, an approval threshold above a rupee amount,
and hard denies on revoked-consent and terminal-failure classes.

## 10. The recovery graph

LangGraph, typed `RecoveryState`, `AsyncPostgresSaver` checkpointer, one thread per recovery case.

```
ingest ─▶ classify ─▶ enrich ─▶ diagnose(LLM) ─▶ score ─▶ plan(LLM)
                                                            │
                                    ┌───────────────────────┘
                                    ▼
                              authorise ──denied──▶ terminate
                                    │
                              step-up required ──▶ [interrupt: AFA] ──▶ authorise
                                    │
                                    ▼
                              policy_eval ──deny──▶ terminate
                                    │
                              requires_approval ──▶ [interrupt: HITL] ──▶ execute
                                    │
                                    ▼
                                 execute ─▶ observe ─▶ (settled? ─▶ close)
                                    ▲                      │
                                    └────── reschedule ◀───┘   (stopping rules)
```

Two distinct interrupt kinds — `AFA_STEP_UP` (waiting on the customer) and `HUMAN_APPROVAL`
(waiting on the merchant's operator) — both durable across a full process restart. Approval
resolution takes a row lock and an optimistic version check so two reviewers cannot double-approve.

## 11. Evidence

The bar is *measured money recovered across batches*. Anvil answers the follow-up question a judge
will actually ask — "would those payments have succeeded anyway?" — with a randomised control arm.

At-risk cases are deterministically assigned by hash to one of three arms:

- **`control`** — no intervention at all. Establishes the natural self-cure rate.
- **`baseline`** — industry-standard fixed-schedule dunning (retry on day 1/3/5, identical email).
- **`anvil`** — the full agent.

Reported per batch: recovery rate and recovered value per arm, **lift over control** and **lift
over baseline** with bootstrap confidence intervals, cost per recovered rupee (model spend +
concessions + channel cost), a breakdown by failure class, and the concession efficiency ratio.
Anywhere the confidence interval crosses zero, the dashboard says so rather than quietly rounding
up. Honest metrics are explicitly part of this track's bar.

## 12. Compliance

**DPDPA 2023.** Consent is a first-class table keyed by `(data_principal, purpose, notice_version)`
with grant and withdrawal timestamps. Every channel send performs a real-time consent lookup for
its specific purpose and fails closed. Withdrawal publishes an erasure event to the outbox;
workers expunge PII from read models, model-facing context and channel logs with exponential
backoff, routing failures to a DLQ for manual inspection. Ledger and audit rows are *not* deleted —
they are tombstoned with PII replaced by irreversible tokens, preserving financial-record integrity
while honouring erasure.

**RBI.** No raw PAN is ever stored or logged; card references are tokens. AFA step-up is modelled
as a real graph interrupt rather than assumed away. Contact-frequency caps and quiet hours are
enforced deterministically in the policy engine.

**PII and the model.** A redaction layer sits between the application and Anthropic: PAN (with
Luhn validation), UPI VPAs, phone numbers, emails, account numbers and IFSC codes are replaced with
stable pseudonyms before any prompt leaves the process, and rehydrated on the way back only where
needed for rendering. The audit log stores the redacted form.

## 13. Failure modes, and what happens

| Failure | Behaviour |
|---------|-----------|
| Anthropic API down / rate-limited | Exponential backoff with jitter; then the deterministic classifier and a conservative default plan take over. Recovery continues with reduced sophistication — it never stops. |
| Model returns malformed structured output | Pydantic validation fails, retry with the validation error appended; after N attempts the case routes to human review. Never a partial write. |
| Model proposes an out-of-bounds action | Policy engine denies, the denial is logged as a *model-safety event*, and the case escalates. Surfaced on the dashboard as a first-class metric, not hidden. |
| Razorpay API timeout on a debit | Status is genuinely unknown. The case enters `PENDING_RECONCILIATION`; a reconciler polls with the same idempotency key. No blind retry. |
| Duplicate webhook | Unique constraint violation on the event id → `200 OK`, no re-execution. |
| Out-of-order webhook | State transitions are guarded by a monotonic sequence; a stale event is recorded and discarded. |
| Worker crashes mid-case | The checkpointer holds the last committed state; on restart the case resumes from that node. Nothing re-executes that already committed. |
| Two operators approve the same action | Row lock plus version check; the second sees a conflict and a refreshed view. |
| Concession budget exhausted mid-batch | Reservation fails deterministically; the planner is re-invoked with concessions removed from its action space. |

## 14. Judge experience

`docker compose up` boots Postgres, the API, the worker and the console, seeded and ready.
**Offline mode is the default** and requires no Razorpay and no Anthropic key: model calls are
served from recorded fixtures and the simulator drives the world, so the entire demo — including
the batch experiment — is reproducible on any machine, byte for byte, from a fixed seed. Supplying
keys switches on live mode, where a real Razorpay test-mode subscription, order and webhook flow
runs alongside the simulated population.
