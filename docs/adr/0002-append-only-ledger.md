# ADR-0002: An append-only ledger, enforced by the database

- **Status:** Accepted
- **Date:** 2026-09-02

## Context

The most common way a financial system loses money is not fraud. It is an
`UPDATE` — a balance column mutated by two concurrent transactions, or a "quick
fix" to a row that seemed wrong at 2am.

Application-level discipline does not survive contact with a migration script, a
future module that adds its own writer, or an engineer with psql open during an
incident. The rule needs to hold for paths that do not go through the
application.

## Decision

We will store no balances at all. Balances are derived by summing an append-only
`ledger_entries` table, and Postgres triggers **refuse** `UPDATE` and `DELETE` on
`ledger_entries`, `ledger_transactions`, `domain_events` and `audit_records`.
Corrections are made by posting a mirrored reversal transaction that references
the original through `reverses_transaction_id`.

There is one documented escape hatch: a session GUC, `anvil.allow_ledger_mutation`,
which nothing in Anvil ever sets.

## Consequences

A balance cannot drift from the history that produced it, because there is
nothing to drift. Verified against a superuser session: inflating, deleting and
rewriting a posted entry are all refused with an error that names the correct
remedy.

The history shows both that a mistake was made and that it was fixed, which is
the information an auditor actually wants — an edit shows neither.

Reading a balance costs a scan. See ADR-0001 for what to do about that if it
ever matters.

The escape hatch exists because an immutability rule with no documented override
gets dropped in a panic, which is strictly worse than one that must be enabled
explicitly, leaves its intent in the session settings, and can be alerted on.

## Alternatives considered

**A mutable balance column with optimistic locking.** Rejected because the
failure mode is silent: a cached balance that is wrong looks exactly like one
that is right, and you find out during an audit.

**Application-only enforcement.** Rejected because it protects only the paths
that go through the application, and the dangerous paths are the other ones.

**Revoking `UPDATE` and `DELETE` at the role level.** Considered and partially
adopted in spirit. Rejected as the primary mechanism because a superuser
connection — which is what an incident responder uses — bypasses it, and because
a trigger can carry an error message that tells the operator what to do instead.
