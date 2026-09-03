# Glossary

Indian payments vocabulary, plus the terms Anvil invents. Written for someone
competent who has not worked on UPI mandates before.

## Payment rails

**UPI Autopay** — recurring payments over UPI. A customer authorises a *mandate*
once; the merchant then presents debits against it without a PIN each time, up to
the mandate's ceiling.

**e-NACH** — the electronic National Automated Clearing House. Bank-account
direct debits, the older and slower rail. Returns are reported with *NACH return
reason codes* (`01` funds insufficient, `02` account closed, and so on).

**Mandate** — a stored authorisation to debit. It carries a maximum amount, a
frequency, a validity window and a finite number of presentment attempts per
cycle. Anvil treats it as a first-class object that every money-moving action
must present.

**UMN** — Unique Mandate Number. The identifier for a UPI Autopay mandate.
e-NACH's equivalent is a UMRN.

**Presentment** — one attempt to collect against a mandate. The count is finite
per cycle, which is why spending them well is the whole problem.

**AFA** — Additional Factor of Authentication. The RBI requirement that certain
transactions carry a second factor (an OTP, a PIN, a biometric). Anvil models a
*step-up* as a real durable pause in the workflow rather than assuming it away.

**Reserve Pay / SBMD** — Single Block Multi Debit. A customer blocks an amount up
front; the merchant then draws it down across several debits without a fresh PIN.
Modelled as an authorisation whose remaining block is tracked and cannot be
overdrawn.

**UPI Circle** — delegated payment authority: a primary account holder lets a
secondary party transact within limits. Anvil models `DELEGATED_AGENT`
authorisations on this shape.

**UAP** — Unified Agent Protocol. NPCI's **proposed** framework for registering
and authorising AI agents on UPI rails. **Not launched** — expected at Global
Fintech Fest 2026 and still requiring RBI approval. Anvil is designed *for* its
shape and does not claim to integrate with it.

**DPDPA** — India's Digital Personal Data Protection Act, 2023. Requires
purpose-specific, withdrawable consent for processing personal data.

## Recovery vocabulary

**Dunning** — the process of chasing a failed or overdue payment. Traditionally a
fixed schedule of retries and reminders; that fixed schedule is Anvil's
*baseline* arm.

**Self-cure** — a failed payment that recovers with no intervention, because the
customer noticed and paid. Around 20% in Anvil's simulation, and the reason an
uncontrolled recovery figure overstates itself.

**Churn** — the customer leaving rather than paying. The failure mode that makes
naive dunning worse than doing nothing, which is why contact pressure is priced
into the scoring.

**Decline code** — the reason a debit failed, as the issuer reports it. Anvil
recognises 76 across four namespaces and escalates the rest to the model.

## Anvil's own terms

**Failure class** — one of ten closed values that a raw decline code maps to.
Each carries a *retry posture* and a hazard curve. The model may only emit
members of this set.

**Retry curve** — a discrete hazard function giving the probability an attempt
settles, given the class, attempt number, age, hour of day and day of month.
Currently hand-written priors, and measurably over-confident.

**Case** — one at-risk invoice, worked from failure to a terminal outcome. One
LangGraph thread per case.

**Arm** — which experimental treatment a case received: `control`, `baseline` or
`anvil`. Assigned by deterministic hash.

**Concession** — a commercial lever the agent may pull to save a subscription: a
grace period, a partial payment, a plan downgrade or a capped discount. Drawn
against a merchant-authorised budget under a row lock.

**Model-safety event** — the model proposing an action outside the closed action
space. Counted and surfaced as a first-class metric rather than silently
corrected.

**Step-up** — an AFA challenge that pauses the graph until the customer
re-authenticates. Distinct from an *approval*, which pauses for a merchant
operator.

**Bezel** — in the console design system, the nested double-border treatment
reserved for exactly two elements, so that it means "this is the thing that
matters" rather than "this is a card".
