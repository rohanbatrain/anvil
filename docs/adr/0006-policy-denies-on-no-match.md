# ADR-0006: A policy gap blocks an action rather than allowing it

- **Status:** Accepted
- **Date:** 2026-09-02

## Context

A policy engine has four places where it can quietly permit something nobody
intended: rule ordering, escalation that gets overturned, overlapping caps, and
— the dangerous one — an action nobody wrote a rule about.

Most rule engines default to allow, because that is what makes them easy to
adopt. In a system that moves money, the consequence is that *forgetting to
write a rule* is indistinguishable from *deciding to permit something*.

## Decision

Four semantics, each closing one of those holes:

1. **The first matching DENY wins outright and stops evaluation.** A later ALLOW
   cannot rescue a denied action.
2. **REQUIRE_APPROVAL is sticky.** Once any rule escalates to a human, nothing
   can drop it back to an unattended ALLOW. Escalation is a floor, not a vote.
3. **The tightest CAP wins, and caps accumulate.** Adding a rule can only ever
   make a limit stricter.
4. **No match denies.** An action no rule permits is refused.

The fourth is the important one, and it inverts how the default bundle reads: the
permits sit at the bottom, in a short list, and the readable question a merchant
asks of the file is *"what is Anvil allowed to do?"* rather than *"what have we
remembered to forbid?"*

## Consequences

A gap in policy is a blocked action rather than an unbounded one, so forgetting
to write a rule is safe.

An empty bundle denies everything, and says so in those words rather than
behaving like an absent one.

A malformed rule raises rather than evaluating to false. "I could not check this
constraint" must never read as "this constraint passed".

The cost is that adding a genuinely new action type requires adding a permit,
and someone will hit that and be briefly annoyed. That annoyance is the feature.

## Alternatives considered

**Default allow with explicit denies.** Rejected: the failure mode is a silently
permitted action, which is the exact failure this system exists to prevent.

**Treating a malformed rule as non-matching.** Rejected for the same reason —
it converts a corrupt bundle into a permissive one.

**Letting the model evaluate policy at decision time.** Rejected outright. The
model *authors* policy in the compiler; a compiled bundle then executes with no
model anywhere near it, so the thousandth evaluation is identical to the first.
