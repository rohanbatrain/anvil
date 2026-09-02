"""Assembling the recovery graph.

The topology is in ``docs/ARCHITECTURE.md`` section 10. What is worth explaining
here is the routing, because the edges carry as much of the design as the nodes.

**Every path to execution runs authorise -> policy.** There is no edge that
reaches ``execute`` without passing both, and the router is written so that
adding one would be an obvious mistake rather than a subtle one.

**The loop is bounded three ways.** LangGraph's own recursion limit is the
backstop; a planning-round counter in the router is the real guard; and the
policy engine's stopping rules are what normally end a case. Relying on the
recursion limit alone would turn "Anvil gave up" into "the graph hit an internal
ceiling", which is not the same thing and must not be reported as if it were.

**Interrupts are ordinary nodes.** ``step_up`` and ``approval`` call
LangGraph's ``interrupt`` inline rather than being configured with
``interrupt_before``. That is deliberate: the node does real work either side of
the pause -- it creates the challenge or the queue item first, and records the
resolution afterwards -- and splitting that across a config flag would put half
of one decision in the graph definition and half in a node.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from langgraph.graph import END, START, StateGraph

from anvil.domain.enums import AuthorisationDecision, CaseStatus, PolicyEffect
from anvil.graph.deps import Deps
from anvil.graph.nodes import act, close, gate, intake, reason
from anvil.graph.state import RecoveryState, current_action

#: Planning rounds before the router stops the case itself. Reached only when the
#: policy engine's own stopping rules have somehow not fired, so hitting it is a
#: signal that the bundle is missing a rule, and the closure reason says so.
MAX_PLANNING_ROUNDS = 6


def _planning_rounds(state: RecoveryState) -> int:
    return sum(1 for h in state.get("history", []) if h.get("node") == "plan")


def route_after_authorise(state: RecoveryState) -> str:
    action = current_action(state)
    if action is None:
        return "plan"
    decision = action.get("authorisation_decision")
    if decision == AuthorisationDecision.REQUIRES_STEP_UP.value:
        return "step_up"
    if decision == AuthorisationDecision.DENIED.value:
        # Denied actions still reach the policy node, which records the refusal
        # against the immutable "unauthorised-actions-never-execute" rule. That
        # gives one place where every refusal is logged the same way.
        return "policy"
    return "policy"


def route_after_step_up(state: RecoveryState) -> str:
    result = state.get("step_up_result") or {}
    return "policy" if result.get("succeeded") else "observe"


def route_after_policy(state: RecoveryState) -> str:
    action = current_action(state)
    if action is None:
        return "plan"
    effect = action.get("policy_effect")
    if effect == PolicyEffect.DENY.value:
        return "observe"
    if effect == PolicyEffect.REQUIRE_APPROVAL.value:
        return "approval"
    return "schedule"


def route_after_approval(state: RecoveryState) -> str:
    action = current_action(state)
    if action is None:
        return "plan"
    return "observe" if action.get("status") == "rejected" else "schedule"


def route_after_schedule(state: RecoveryState) -> str:
    action = current_action(state)
    if action is None or action.get("status") == "cancelled":
        return "observe"
    return "execute"


def route_after_execute(state: RecoveryState) -> str:
    if state.get("status") == CaseStatus.PENDING_RECONCILIATION.value:
        # Nothing further can be decided until the gateway's answer is known.
        # The reconciler resumes this thread once it is.
        return "close"
    return "observe"


def route_after_observe(state: RecoveryState) -> str:
    status = state.get("status")
    if status == "closing":
        return "close"
    if _planning_rounds(state) >= MAX_PLANNING_ROUNDS:
        return "close"
    if status == "executing" and current_action(state) is not None:
        return "authorise"
    return "plan"


def build_graph(deps: Deps, checkpointer: Any = None) -> Any:
    """Compile the recovery graph over a dependency container.

    ``checkpointer`` is optional so a test can compile and run the graph purely
    in memory. In every real deployment it is an ``AsyncPostgresSaver``, which is
    what makes the two interrupts survive a process restart.
    """
    graph: StateGraph = StateGraph(RecoveryState)

    graph.add_node("ingest", partial(intake.ingest, deps))
    graph.add_node("classify", partial(intake.classify, deps))
    graph.add_node("score", partial(intake.score, deps))
    graph.add_node("diagnose", partial(reason.diagnose, deps))
    graph.add_node("plan", partial(reason.plan, deps))
    graph.add_node("authorise", partial(gate.authorise, deps))
    graph.add_node("step_up", partial(gate.step_up, deps))
    graph.add_node("policy", partial(gate.policy, deps))
    graph.add_node("approval", partial(gate.approval, deps))
    graph.add_node("schedule", partial(act.schedule, deps))
    graph.add_node("execute", partial(act.execute, deps))
    graph.add_node("observe", partial(act.observe, deps))
    graph.add_node("close", partial(close.close, deps))

    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "classify")
    graph.add_edge("classify", "score")
    graph.add_edge("score", "diagnose")
    graph.add_edge("diagnose", "plan")
    graph.add_edge("plan", "authorise")

    graph.add_conditional_edges(
        "authorise",
        route_after_authorise,
        {"policy": "policy", "step_up": "step_up", "plan": "plan"},
    )
    graph.add_conditional_edges(
        "step_up", route_after_step_up, {"policy": "policy", "observe": "observe"}
    )
    graph.add_conditional_edges(
        "policy",
        route_after_policy,
        {"schedule": "schedule", "approval": "approval", "observe": "observe", "plan": "plan"},
    )
    graph.add_conditional_edges(
        "approval",
        route_after_approval,
        {"schedule": "schedule", "observe": "observe", "plan": "plan"},
    )
    graph.add_conditional_edges(
        "schedule", route_after_schedule, {"execute": "execute", "observe": "observe"}
    )
    graph.add_conditional_edges(
        "execute", route_after_execute, {"observe": "observe", "close": "close"}
    )
    graph.add_conditional_edges(
        "observe",
        route_after_observe,
        {"close": "close", "authorise": "authorise", "plan": "plan"},
    )
    graph.add_edge("close", END)

    return graph.compile(checkpointer=checkpointer)


def graph_topology() -> dict[str, list[str]]:
    """The edges, as data. Used by the docs build to render the diagram.

    Keeping one machine-readable description means the picture in the README
    cannot drift away from the graph that actually runs.
    """
    return {
        "ingest": ["classify"],
        "classify": ["score"],
        "score": ["diagnose"],
        "diagnose": ["plan"],
        "plan": ["authorise"],
        "authorise": ["policy", "step_up", "plan"],
        "step_up": ["policy", "observe"],
        "policy": ["schedule", "approval", "observe", "plan"],
        "approval": ["schedule", "observe", "plan"],
        "schedule": ["execute", "observe"],
        "execute": ["observe", "close"],
        "observe": ["close", "authorise", "plan"],
        "close": ["END"],
    }
