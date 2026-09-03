# ADR-0003: LangGraph as the durable spine, Claude in the reasoning nodes

- **Status:** Accepted
- **Date:** 2026-09-02

## Context

A recovery case is not a conversation. It runs for weeks, pauses twice for
different humans, survives process restarts, and must be replayable afterwards
to explain itself. A linear chain that restarts on failure loses the case.

There was also a cultural pull in the other direction: Razorpay's own Agent
Studio is built on Anthropic's Claude Agent SDK, and matching their stack is a
real signal.

## Decision

We will use **LangGraph for control flow, typed state, the Postgres
checkpointer, interrupt/resume and replay**, with **Claude models doing the
reasoning inside individual nodes**. The durability properties come from the
graph; the judgement comes from the model.

## Consequences

Both human pauses are genuinely durable. The checkpoint is committed before the
node yields, so the process can be killed mid-pause and the case resumes exactly
there, on a different machine, days later. This is tested, not assumed.

Time-travel debugging comes free: every state transition is a checkpoint, so a
case can be rewound, inspected and forked.

We carry a heavier dependency than an agent loop would need, and LangGraph's
state merging has a sharp edge we were cut by — a field not declared in the
state TypedDict is **silently dropped**. That cost us a real bug in which every
merchant appeared to be in review-first mode. The lesson is recorded in
`anvil/graph/state.py` next to the field.

We can still say the thing worth saying in an interview: *why a checkpointed
graph, and not an agent loop, is the correct shape for money.*

## Alternatives considered

**Claude Agent SDK end to end.** Maximum alignment with Razorpay's stack, and
the resulting code would look at home in their repository. Rejected because we
would have had to build durable checkpointing, interrupt/resume and replay
ourselves — which is precisely the hard part, and precisely what LangGraph
already does.

**A hand-rolled durable state machine.** Explicit states, a transition table, a
Postgres journal. Genuinely attractive, and the strongest possible determinism
claim. Rejected on time: rebuilding checkpointing correctly is a week, and
framework avoidance reads as unfamiliarity unless the reasoning is visible.

**A linear chain.** Rejected outright. A step that fails mid-execution restarts
the whole chain, losing context and re-spending money.
