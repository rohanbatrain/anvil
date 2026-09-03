# ADR-0007: Authorisation is checked before policy, and fails closed

- **Status:** Accepted
- **Date:** 2026-09-02

## Context

Two different questions get conflated in agent systems: *does the right to do
this exist?* and *should we exercise that right?* They have different answers,
different owners, and different failure modes.

A mandate is a stored fact about what a customer authorised. A policy is a
merchant's judgement about what to do with that authority. Running them in the
wrong order means writing policy rules about actions the merchant was never
authorised to take.

## Decision

Every action passes **authorisation first, then policy**, and there is no edge in
the graph that reaches the executor while skipping either. Authorisation is a
structural check against a stored authorisation object — a UPI Autopay or e-NACH
mandate, a Reserve Pay block, or delegated agent authority — and it is **total**:
there is no branch that falls through to "allow".

An action within the principal's own limits but outside a delegated agent's cap
returns `REQUIRES_STEP_UP` rather than `DENIED`, which interrupts the graph and
waits for the customer to re-authenticate.

## Consequences

"Bounded" becomes a precondition of execution rather than a policy convention.

The authorisation result is itself a **fact the policy engine reads**, so the
immutable rule `unauthorised-actions-never-execute` has a value to test. Actions
that move no money still pass through the check for exactly this reason — an
absent value would make that rule silently vacuous.

Every denial records which of the ten `DenialReason` values applied, so an
operator can tell "the mandate expired" from "the amount exceeded the delegated
cap", which imply different remedies.

The AFA step-up is modelled as a real durable pause rather than assumed away.
That is more work than treating it as a denial, and it is the difference between
acknowledging RBI's additional-factor requirement and implementing it.

## Alternatives considered

**Policy first, authorisation at execution time.** Rejected because it invites
policy rules that reason about authority the merchant does not hold, and because
a check at execution time is a check under time pressure.

**One combined "can we do this" check.** Rejected because the two questions have
different owners: the mandate registry is not the merchant's to configure, and
the policy bundle is not the customer's.

**Treating an over-cap delegated action as denied.** Simpler, and it loses the
recovery entirely. Rejected because the customer would very often have approved
it, and asking is the whole point of a step-up.
