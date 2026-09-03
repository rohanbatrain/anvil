# Compliance

What Anvil does about DPDPA and RBI requirements, and — equally important — what
it does not claim.

## DPDPA 2023

India's Digital Personal Data Protection Act requires that processing of personal
data rests on consent that is **specific, informed and withdrawable**.

**Consent is per purpose, never general.** `consent_receipts` is keyed on
`(data principal, purpose, notice version)`. `MessagePurpose` distinguishes a
payment-failure notice from a promotional win-back, and the two require separate
consent — a win-back offer cannot ride on a service-message consent. Every
channel send performs a real-time lookup for the *specific* purpose it is about
to serve, and fails closed.

**Withdrawal is a new row, never an update.** The history of what was permitted,
and when, survives. Three of the five immutable policy rules exist to enforce
this, and the natural-language compiler cannot weaken or remove them.

**Suppressed messages are persisted with their reason.** "We did not contact this
person, and here is why" is the record a regulator asks for. Discarding it is how
a compliant system becomes an unprovable one.

**Erasure tombstones rather than deletes.** Withdrawal publishes an erasure event
to the outbox; workers expunge PII from read models, model-facing context and
channel logs with exponential backoff, dead-lettering failures for manual
inspection. Ledger and audit rows are **not** deleted — PII is replaced with
irreversible tokens, honouring erasure without destroying financial records.

**Nothing reaches a model unredacted.** PANs (Luhn-validated), UPI VPAs, phone
numbers, emails, account numbers and IFSC codes are replaced with stable
pseudonyms before any prompt leaves the process.

## RBI

**No raw card data.** No field in the schema holds a PAN. Card references are
tokens.

**AFA is modelled, not assumed away.** An action within the principal's authority
but outside a delegated agent's cap returns `REQUIRES_STEP_UP`, which pauses the
graph on a durable checkpoint until the customer re-authenticates. Implementing
the pause is more work than treating it as a denial, and it is the difference
between acknowledging the requirement and satisfying it.

**Contact discipline is deterministic.** Quiet hours (21:00–08:00 IST) and
rolling frequency caps are policy rules, not heuristics. A step-up challenge is
exempt from quiet hours because the customer is waiting on it in real time;
nothing else is, and that exemption is written down rather than implicit.

## Agentic protocols — what is and is not live

Worth being precise about, because overclaiming here is the fastest way to lose
credibility with a payments panel.

**UPI Autopay and e-NACH are production rails today.** Anvil models mandates
against both.

**UPI Circle and Reserve Pay exist.** Anvil models delegated agent authority and
Single Block Multi Debit blocks as first-class authorisation objects.

**UAP has not launched.** NPCI's Unified Agent Protocol — the framework that
would let a *verified AI agent* transact on those rails — is expected to be
unveiled at Global Fintech Fest 2026 and still requires RBI approval.

Anvil therefore **does not integrate with UAP and does not claim to**. It models
authorisation in the shape UAP describes, so that when the protocol lands the
registry gains an issuer rather than a redesign. Every statement about UAP in
this repository describes a proposed standard.

## What is not implemented

The `audit` module — the redaction gate, the immutable trail's writer, the outbox
relay and time-travel replay — is **specified and not built**. The consent and
erasure tables exist in the schema and the policy rules that depend on them are
enforced, but the asynchronous erasure workers are not written.

Nothing here has been reviewed by a lawyer. It is an engineer's reading of the
requirements, built to be checkable rather than to be asserted.
