# ADR-0009: An unknown gateway outcome is a state, not a failure

- **Status:** Accepted
- **Date:** 2026-09-02

## Context

When a debit request to a payment gateway times out, the outcome is genuinely
unknown. The request may have reached the issuer and taken the customer's money;
it may not have. The two most natural responses are both wrong.

Treating it as a failure and retrying is how a customer gets charged twice.
Treating it as a failure and writing the receivable off understates what the
merchant is owed, and has to be reversed the moment it resolves.

## Decision

`unknown` is a **first-class outcome** of the gateway port, alongside `settled`
and `failed`. A case whose last attempt returned no answer parks in
`PENDING_RECONCILIATION`, which is deliberately **not** a terminal status.

Nothing is posted to the ledger. Nothing is written off. A reconciler polls the
gateway with the **original idempotency key** until the outcome is known.

## Consequences

Recording a recovery we cannot confirm is worse than recording nothing, and this
makes that structural rather than aspirational.

The graph terminates but the case does not, which required a distinction in the
tests between "the graph reached a resting state" and "the case reached a
terminal state". That distinction is correct and worth having.

A case can sit in reconciliation indefinitely if the gateway never answers. The
reconciler has bounded retries and then escalates to a human, because an
unresolved payment is a person's problem eventually.

This was found by running the guided tour, which showed a timed-out case being
reported as `abandoned` with the money written off. The tour paid for itself in
its first run.

## Alternatives considered

**Retry on timeout.** Rejected: this is the double-charge path, and it is the
single most common way naive payment integrations lose customer trust.

**Treat as failed and let the next cycle sort it out.** Rejected because it
writes off money that may already be collected, and because "the next cycle"
means a month of a wrong balance.

**Block until resolved.** Rejected because it holds a worker on an external
system's availability, and because the answer may take hours.
