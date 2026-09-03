# ADR-0001: A modular monolith with an event-sourced financial core

- **Status:** Accepted
- **Date:** 2026-09-02

## Context

Anvil has to hold two properties that usually pull against each other. It needs
a **provably complete audit trail** — a regulator or a merchant must be able to
ask why any rupee moved and get an answer months later — and it needs to be
**runnable by a reviewer in one command**, because a system nobody can start is
a system nobody will assess.

The obvious architectures each sacrifice one of those. A plain CRUD service is
trivial to run and has no audit story beyond hoping nobody edited a row. A full
event-sourced CQRS system has a magnificent audit story and introduces
projection lag, which makes the console subtly wrong in ways that are hard to
explain. A control-plane/data-plane split is what this would eventually become
at scale, and costs five services in a demo.

## Decision

We will build a **modular monolith with an event-sourced financial core**: a
single Postgres holding an append-only `ledger_entries` table and an append-only
`domain_events` log, written in the *same transaction* as every state change via
a transactional outbox. Balances and case state are derived, never mutated. The
FastAPI process handles webhooks and the console API; a separate worker process
runs the LangGraph executor. Two processes, one database, one schema.

## Consequences

The event log and the read model commit atomically, so the log can never
disagree with the state it describes — which buys event sourcing's replay
guarantees without its eventual consistency.

A runaway model call cannot starve webhook ingestion, and workers scale
independently, for the cost of one extra process rather than five extra
services.

Deriving balances is a scan rather than a lookup. At this scale that is free; at
a much larger one the correct next step is a materialised rollup with the
derivation retained as the authority to check it against — explicitly *not* a
mutable balance column.

The module boundaries are already the service boundaries and the event log is
already the integration contract, so splitting this later is a deployment change
rather than a rewrite. That is also the honest answer to "how would you scale
this?", and a better one than having pre-split it and hoping nobody asked why.

## Alternatives considered

**Full CQRS with separate read models.** Rejected because projection lag makes a
console show stale approvals, and an operator who approves an action twice
because the queue had not caught up is a real incident. The audit benefit was
obtainable without it.

**Control plane / data plane split.** Genuinely better at scale and the
direction this grows in. Rejected for now because distributed human-in-the-loop
approval across a service boundary needs distributed locking to stop two
reviewers double-approving, where the monolith needs one `SELECT … FOR UPDATE`
and a version column. Same correctness guarantee, a fraction of the machinery.

**Plain CRUD with an audit table written alongside.** Rejected because "written
alongside" is exactly the thing that silently stops happening. If the audit
record is not in the same transaction as the change, it is a hope rather than a
guarantee.
