# ADR-0008: The orchestrator imports nothing it orchestrates

- **Status:** Accepted
- **Date:** 2026-09-02

## Context

The recovery graph touches everything: classification, scoring, authorisation,
policy, the ledger, channels, the gateway and the model. If it imported all of
them, the dependency arrows would point from the orchestrator into every leaf,
it would be untestable without the whole system standing up, and no two people
could build it in parallel.

## Decision

The graph imports **none** of the modules it drives. `anvil/graph/ports.py`
declares twelve narrow Protocols describing exactly the behaviour it needs, and
a composition root supplies concrete implementations.

The slice each port exposes *is* the contract. `LedgerPort` is the one to read:
it has no `post` method and no way to construct an arbitrary entry. The
orchestrator can record four economic events and nothing else, so a bug in a node
cannot invent a posting the chart of accounts never anticipated.

## Consequences

Every node is testable against a hand-written double in a few lines, with no
database, no network and no model. The 39 graph tests run in under a second.

A dependency that fails is a stub that raises, which is how the degradation
paths get exercised honestly rather than assumed.

"How much authority does the agent have over the books?" is answerable by
reading one file.

The cost is a layer of indirection and a second definition of each shape. That
is real, and it is also what let the graph be written while six other modules
were still being built.

## Alternatives considered

**Direct imports.** Simpler to read in the small. Rejected because it inverts
the dependency direction and makes the orchestrator's blast radius the whole
system.

**Dependency injection through a container or registry.** Rejected as heavier
than a frozen dataclass of Protocols, and because a global registry makes it
possible to reach something the port does not expose.

**Passing dependencies through LangGraph's `configurable`.** Rejected because it
is untyped, so a missing dependency becomes a `KeyError` at node execution time
rather than a construction error at startup.
