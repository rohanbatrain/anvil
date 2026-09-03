# The ten invariants

The financial guarantees Anvil makes, what enforces each one, and the test that
proves it. Numbered as in [the architecture document](../explanation/architecture.md) §6.

Run them all:

```bash
.venv/bin/python -m pytest -m invariant -q
```

---

## 1. No balance is ever mutated

Balances are derived by summing append-only `ledger_entries`. There is no stored
balance anywhere in the schema, so there is nothing that can drift from the
history that produced it.

**Enforced by:** the absence of any balance column; Postgres triggers rejecting
`UPDATE` and `DELETE` on `ledger_entries`, `ledger_transactions`, `domain_events`
and `audit_records`.
**Verify:** `psql` and try it. The error names the correct remedy.
**Reasoning:** [ADR-0002](../adr/0002-append-only-ledger.md)

## 2. Every ledger transaction balances to zero

Debits equal credits, per transaction, per currency, checked **before** anything
is written.

**Enforced by:** `anvil.ledger.posting.validate`, which every builder calls and
which `post` calls again before touching a session.
**Test:** `test_every_builder_produces_a_balanced_transaction` — a property test
over every builder and every amount. If it can be made to fail, money can be
created or destroyed.

## 3. Money is integer minor units

`Money` is `(int paise, Currency)`. Floats are refused by the type itself.

**Enforced by:** `Money.__post_init__` and `from_major`, which reject `float`.
**Test:** `test_money_cannot_be_built_from_float`, and
`test_allocation_conserves_every_paisa` — splitting money never creates or
destroys a minor unit.

## 4. Every inbound webhook is processed at most once

**Enforced by:** a unique constraint on Razorpay's `x-razorpay-event-id`. A
duplicate raises an `IntegrityError` (Postgres `23505`) which the handler
translates into a plain `200 OK` with no business logic run.

The duplicate is detected by **catching the constraint violation**, not by a
prior `SELECT` — a pre-check is a race, and the constraint is the mechanism.

## 5. Every outbound money-moving call carries an idempotency key

Caller-generated, and stable across retries because it is derived from the
**intent** and never from the attempt.

**Enforced by:** `anvil.core.ids.idempotency_key`; the gateway client requires
one on every mutating call.
**Test:** `test_idempotency_key_depends_only_on_intent`. A key that varied per
call would turn a network retry into a double charge.

## 6. No action executes without a valid authorisation

**Enforced by:** `anvil.mandates.authorise`, which is **total** — there is no
branch that falls through to "authorised". Every denial records which of the ten
`DenialReason` values applied.
**Reasoning:** [ADR-0007](../adr/0007-authorisation-before-policy.md)

## 7. No action executes without a policy pass

The decision, the matched rule and the bundle version are persisted with the
action, so "why was this allowed?" is answerable months later.

**Enforced by:** the graph's routing — no edge reaches `execute` without passing
`authorise` then `policy`.
**Test:** `test_every_executed_action_passed_authorisation_and_policy`, checked
structurally against the audit trail's ordering.

## 8. Concessions draw against a reserved budget

Reservation happens under `SELECT … FOR UPDATE` on the budget row **before** the
action, so two concurrent cases cannot jointly overspend.

**Enforced by:** `anvil.ledger.reservations.reserve`.
**Test:** `test_two_concurrent_concessions_cannot_jointly_overspend` and a
property test that a reservation is never allowed beyond headroom.
**Reasoning:** [ADR-0004](../adr/0004-bounded-authority.md)

## 9. Every state transition is replayable

The LangGraph checkpoint at each node plus the event log reconstruct any case at
any point in its life.

**Enforced by:** the Postgres checkpointer; `domain_events` written in the same
transaction as the state change they describe.

## 10. The audit log contains no raw PII

Redaction happens **before** persistence, not on read — redacting on read would
mean the raw value had already been written somewhere.

**Enforced by:** `anvil.audit.recorder`, which scans outgoing detail for PII
patterns and refuses to write rather than sanitising quietly.

---

## What is not guaranteed

Worth stating, because a list of guarantees invites the assumption that
everything else is covered too.

There is no guarantee about **delivery** of outreach — a channel may accept a
message and never deliver it, and Anvil records what it dispatched rather than
what arrived. There is no guarantee that a **recovered** case stays recovered; a
chargeback later is a new event. And there is no guarantee that the **scheduler's
probabilities are accurate** — that is measured rather than assumed, and
currently they are over-confident by about ten points.
