# Architecture decision records

Every consequential decision in Anvil, with the reasoning that produced it and
the alternatives that lost. Kept in the repository and versioned with the code,
so the record moves when the code does.

The point of these is the **Alternatives considered** section. What was decided
is usually inferable from the code; what was rejected, and why, is not — and
that is the part a reviewer, or a maintainer in a year, actually needs.

Decisions that turned out to be wrong are marked superseded rather than edited.
The record of having changed one's mind is the useful part.

| # | Decision | Status |
|---|---|---|
| [0001](0001-modular-monolith-event-sourced-core.md) | A modular monolith with an event-sourced financial core | Accepted |
| [0002](0002-append-only-ledger.md) | An append-only ledger, enforced by the database | Accepted |
| [0003](0003-langgraph-durable-spine.md) | LangGraph as the durable spine, Claude in the reasoning nodes | Accepted |
| [0004](0004-bounded-authority.md) | Bounded commercial authority under a hard-capped budget | Accepted |
| [0005](0005-no-llm-for-retry-timing.md) | Retry timing is a dynamic program, not a model call | Accepted |
| [0006](0006-policy-denies-on-no-match.md) | A policy gap blocks an action rather than allowing it | Accepted |
| [0007](0007-authorisation-before-policy.md) | Authorisation is checked before policy, and fails closed | Accepted |
| [0008](0008-graph-depends-only-on-protocols.md) | The orchestrator imports nothing it orchestrates | Accepted |
| [0009](0009-unknown-gateway-outcomes.md) | An unknown gateway outcome is a state, not a failure | Accepted |
| [0010](0010-three-arm-evidence-model.md) | Recovery is measured against a control arm, or not claimed | Accepted |
| [0011](0011-buildless-console.md) | The console is served by the API with no build step | Accepted |
| [0012](0012-report-the-losing-result.md) | Report the result that makes the project look worse | Accepted |
| [0013](0013-native-postgres-for-development.md) | Native Postgres for development, not Docker | Accepted |

New decisions start from [`000-template.md`](000-template.md).
