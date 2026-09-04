"""A live view of one recovery case moving through the graph, node by node.

This endpoint exists for a reason the rest of the API does not serve. Anvil's
architecture is defensible on paper and genuinely hard to *see*: thirteen nodes,
two durable interrupts, three deterministic gates and a model that proposes but
never decides. Reading that is not the same as watching a case move through it.

So this streams the real thing. LangGraph's ``astream(stream_mode="updates")``
yields one event per node with the state delta, and every event here is a node
that actually executed against the real classifier, the real scheduler, the real
mandate check and the real policy bundle. Nothing is replayed from a script.

The scenarios are the teaching device. Each one picks a case from the seeded
world whose ground truth produces a particular shape -- a technical decline that
clears on a fast retry, an insufficient-funds case that waits eleven days for
payday, a revoked mandate that is refused outright, a model that goes down
mid-case, a model that proposes something outside the closed action space. A
reader who watches four of these understands the system better than one who
reads the architecture document twice.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Query
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from sse_starlette.sse import EventSourceResponse

from anvil.api.state import get_state
from anvil.core.clock import FrozenClock, to_ist
from anvil.domain.enums import ActionType, FailureClass
from anvil.domain.money import Money
from anvil.graph.build import build_graph, graph_topology
from anvil.graph.deps import Deps
from anvil.graph.state import initial_state
from anvil.simulator import world as simworld
from anvil.simulator.world import SimCase

router = APIRouter(prefix="/api/journey", tags=["journey"])

#: How long to dwell on each node before emitting the next. Real execution is
#: far faster than a person can read, and a wall of text that appears at once
#: teaches nothing. This is presentation, and it is the only thing here that is.
STEP_DELAY_SECONDS = 0.65


@dataclass(frozen=True, slots=True)
class Scenario:
    """One thing worth watching, and what it is meant to show."""

    key: str
    title: str
    teaches: str
    failure_class: FailureClass | None = None
    model_available: bool = True
    #: Force the planner to propose something outside the closed action space.
    inject_out_of_bounds: bool = False
    #: Force the gateway to return an unknown outcome.
    gateway_timeout: bool = False
    #: Leave the merchant in review-first, so the case pauses for a human.
    review_first: bool = False


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        key="fast-retry",
        title="A technical decline, recovered in hours",
        teaches=(
            "The cheapest recovery there is. The customer could always pay; the rail could "
            "not take the money. Watch the scheduler pick an hour six hours out, clear of "
            "the overnight issuer maintenance window, and the debit settle."
        ),
        failure_class=FailureClass.ISSUER_TECHNICAL,
    ),
    Scenario(
        key="payday",
        title="Insufficient funds, and the patience to wait for payday",
        teaches=(
            "The decision that justifies the whole scheduler. A balance failure does not "
            "clear tomorrow, so the optimiser holds out nearly a fortnight for a "
            "salary-credit day. A greedy retry loop burns its attempts and gets nothing."
        ),
        failure_class=FailureClass.INSUFFICIENT_FUNDS,
    ),
    Scenario(
        key="terminal",
        title="A revoked mandate, refused rather than retried",
        teaches=(
            "The customer cancelled. There is no longer an authorisation to debit against, "
            "so retrying is not merely futile but unauthorised. Watch the policy engine "
            "refuse it and the case close as churn rather than as a payment failure."
        ),
        failure_class=FailureClass.MANDATE_REVOKED,
    ),
    Scenario(
        key="degraded",
        title="The language model is down, and recovery continues",
        teaches=(
            "The failure path most systems never test. With Claude entirely unreachable "
            "the case still classifies, plans and recovers, on a deterministic fallback "
            "that never offers a concession -- because pricing one was exactly the "
            "judgement the model was there to make."
        ),
        failure_class=FailureClass.ISSUER_TECHNICAL,
        model_available=False,
    ),
    Scenario(
        key="out-of-bounds",
        title="The model proposes something it must not",
        teaches=(
            "The model asks to wire the customer money, which is not in the closed action "
            "space. It is refused before the executor sees it and counted as a "
            "model-safety event, which the batch report surfaces as a first-class metric "
            "rather than hiding."
        ),
        failure_class=FailureClass.INSUFFICIENT_FUNDS,
        inject_out_of_bounds=True,
    ),
    Scenario(
        key="human-approval",
        title="The graph stops and waits for a person",
        teaches=(
            "A real durable interrupt. The checkpoint commits before the node yields, so "
            "the process can be killed here and the case resumes exactly where it stopped. "
            "The operator sees the agent's own reasoning before deciding."
        ),
        failure_class=FailureClass.ISSUER_TECHNICAL,
        review_first=True,
    ),
    Scenario(
        key="unknown-outcome",
        title="The gateway times out and nothing is assumed",
        teaches=(
            "The outcome is genuinely unknown: the customer may or may not have been "
            "charged. Nothing is posted to the ledger, nothing is written off, and the "
            "case parks for reconciliation under the original idempotency key. Retrying "
            "here is how a customer gets charged twice."
        ),
        failure_class=FailureClass.ISSUER_TECHNICAL,
        gateway_timeout=True,
    ),
)

_BY_KEY = {s.key: s for s in SCENARIOS}

#: What each node is for, in one line. The console shows this beside the node as
#: it lights up, so a reader learns the topology by watching it run.
NODE_PURPOSE: dict[str, str] = {
    "ingest": "Open the case and recognise the receivable on the ledger",
    "classify": "Map the raw reason code to a failure class — rules first, model only if they fail",
    "score": "Recovery likelihood, churn risk and priority. Deterministic, no model",
    "diagnose": "Infer what is actually wrong: can they pay, do they intend to",
    "plan": "Choose actions from a closed set, under a live concession budget",
    "authorise": "Is there a stored right to do this? Structural, and it fails closed",
    "step_up": "Paused: the customer must re-authenticate before this can proceed",
    "policy": "Is it permitted, and how much of it? Deterministic. No match denies",
    "approval": "Paused: a person must decide before any money moves",
    "schedule": "Solve for the best hour — a dynamic program over the hazard curve",
    "execute": "Do the thing. Idempotency key attached, outcome recorded honestly",
    "observe": "What did that mean? Continue, re-plan, or stop",
    "close": "Terminal. Write off anything genuinely lost",
}

_GATE_NODES = frozenset({"authorise", "policy"})
_PAUSE_NODES = frozenset({"step_up", "approval"})
_MODEL_NODES = frozenset({"diagnose", "plan"})


@router.get("/scenarios")
async def scenarios() -> dict[str, Any]:
    """The menu, plus the topology the console draws."""
    return {
        "scenarios": [{"key": s.key, "title": s.title, "teaches": s.teaches} for s in SCENARIOS],
        "topology": graph_topology(),
        "nodes": [
            {
                "name": name,
                "purpose": purpose,
                "kind": (
                    "pause"
                    if name in _PAUSE_NODES
                    else "gate"
                    if name in _GATE_NODES
                    else "model"
                    if name in _MODEL_NODES
                    else "step"
                ),
            }
            for name, purpose in NODE_PURPOSE.items()
        ],
    }


def _pick_case(scenario: Scenario) -> SimCase:
    """A case from the seeded world whose ground truth suits the scenario.

    Falls back to any case rather than failing: a scenario that cannot find its
    ideal example is still worth watching, and an empty screen teaches nothing.
    """
    state = get_state()
    if scenario.failure_class is not None:
        for case in state.cases:
            if case.true_failure_class is scenario.failure_class:
                return case
    return state.cases[0]


class _OutOfBoundsModel(simworld._ClassifyingModel):
    """A model that proposes an action outside the closed set, on purpose.

    Used only by the out-of-bounds scenario. The point is to show the refusal,
    and a refusal nobody can trigger is a claim rather than a demonstration.
    """

    async def plan(self, **_: Any) -> dict[str, Any]:
        self.cost += 120
        return {
            "strategy": "an invented step alongside a legitimate one",
            "steps": [
                {
                    "action_type": "wire_the_customer_money",
                    "amount_minor": 50_000,
                    "rationale": "Not in the closed action space. This must be refused.",
                    "confidence": 91,
                },
                {
                    "action_type": ActionType.RETRY_DEBIT.value,
                    "amount_minor": 149900,
                    "rationale": "A legitimate retry, which should survive the filter.",
                    "confidence": 74,
                },
            ],
        }


class _TimingOutGateway(simworld._Gateway):
    """A gateway whose answer never arrives."""

    async def attempt_debit(self, **_: Any) -> dict[str, Any]:
        return {"outcome": "unknown", "detail": "gateway timeout, outcome unknown"}


def _build(
    scenario: Scenario, case: SimCase, use_claude: bool = False
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    console = get_state()
    clock = FrozenClock(case.failed_at)
    outcome = console.world._blank_outcome(case)
    recorder = simworld._Recorder(outcome)

    if use_claude and scenario.model_available and not scenario.inject_out_of_bounds:
        from anvil.core.config import get_settings

        if get_settings().anthropic_api_key.get_secret_value():
            from anvil.llm.client import ClaudeModel

            model: Any = ClaudeModel()
        else:
            model = simworld._ClassifyingModel(case, console.seed)
    elif scenario.inject_out_of_bounds:
        model = _OutOfBoundsModel(case, console.seed)
    elif scenario.model_available:
        model = simworld._ClassifyingModel(case, console.seed)
    else:
        model = simworld._FallbackModel()

    gateway: Any = (
        _TimingOutGateway(console.world, case, clock, outcome)
        if scenario.gateway_timeout
        else simworld._Gateway(console.world, case, clock, outcome)
    )

    deps = Deps(
        clock=clock,
        classifier=simworld._Classifier(),
        scheduler=simworld._Scheduler(clock),
        scoring=simworld._Scoring(),
        model=model,
        authorisation=simworld._Authorisation(case),
        policy=simworld._Policy(case),
        approvals=simworld._AutoApproval(),
        ledger=recorder,
        gateway=gateway,
        channels=simworld._Channels(console.world, case, outcome),
        audit=recorder,
        cases=recorder,
        allowed_actions=tuple(a.value for a in ActionType),
    )
    graph = build_graph(deps, checkpointer=MemorySaver())
    config = {
        "configurable": {"thread_id": f"journey:{scenario.key}:{case.case_id}"},
        "recursion_limit": 60,
    }
    seed = initial_state(
        case_id=case.case_id,
        thread_id=config["configurable"]["thread_id"],
        merchant_id=console.population.merchant_id,
        customer_id=case.customer.customer_id,
        subscription_id=case.customer.subscription_id,
        amount_at_risk_minor=case.amount.minor,
        subscription_mrr_minor=case.amount.minor,
        original_failure_at=case.failed_at.isoformat(),
        correlation_id=case.case_id,
        raw_failure_code=case.raw_code,
        raw_failure_description=case.narration,
        bank_narration=case.narration,
        consent_state="granted",
        merchant_review_first=scenario.review_first,
        customer_tenure_days=case.customer.tenure_days,
        customer_lifetime_value_minor=case.customer.lifetime_value.minor,
        budget_headroom_minor=50_000_00,
        customer_concession_headroom_minor=case.amount.minor // 2,
        preferred_language=case.customer.traits.language,
    )
    return graph, config, seed


def _snapshot(merged: dict[str, Any]) -> dict[str, Any]:
    """The handful of values worth showing beside the graph as it runs."""
    return {
        "status": merged.get("status"),
        "failure_class": merged.get("failure_class"),
        "classified_deterministically": merged.get("classified_deterministically"),
        "recovery_likelihood": merged.get("recovery_likelihood"),
        "churn_risk": merged.get("churn_risk"),
        "attempts": merged.get("attempts_made", 0),
        "contacts": merged.get("contacts_made", 0),
        "recovered": Money(int(merged.get("amount_recovered_minor", 0))).format(),
        "at_risk": Money(int(merged.get("amount_at_risk_minor", 0))).format(),
        "conceded": Money(int(merged.get("concession_granted_minor", 0))).format(),
        "degraded": bool(merged.get("degraded", False)),
        "degraded_reason": merged.get("degraded_reason"),
        "safety_events": merged.get("model_safety_events", 0),
        "next_action_at": merged.get("next_action_at"),
    }


async def _events(scenario: Scenario, use_claude: bool = False) -> AsyncIterator[dict[str, str]]:
    case = _pick_case(scenario)
    graph, config, seed = _build(scenario, case, use_claude=use_claude)

    yield {
        "event": "case",
        "data": json.dumps(
            {
                "scenario": scenario.key,
                "title": scenario.title,
                "teaches": scenario.teaches,
                "case_id": case.case_id,
                "customer": case.customer.display_name,
                "bank": case.customer.bank.name,
                "mandate": case.customer.authorisation.auth_type.value,
                "amount": case.amount.format(),
                "raw_code": case.raw_code or "(none)",
                "narration": case.narration,
                "code_is_unmapped": case.code_is_unmapped,
                "failed_at": to_ist(case.failed_at).strftime("%a %d %b %H:%M IST"),
                "true_failure_class": case.true_failure_class.value,
            }
        ),
    }

    merged: dict[str, Any] = dict(seed)
    resumes = 0

    async def pump(stream: Any) -> AsyncIterator[dict[str, str]]:
        nonlocal merged
        async for chunk in stream:
            for node, update in chunk.items():
                if node == "__interrupt__":
                    continue
                if isinstance(update, dict):
                    merged.update(update)
                history = merged.get("history", [])
                summary = history[-1]["summary"] if history else ""
                await asyncio.sleep(STEP_DELAY_SECONDS)
                yield {
                    "event": "node",
                    "data": json.dumps(
                        {
                            "node": node,
                            "purpose": NODE_PURPOSE.get(node, ""),
                            "summary": summary,
                            "kind": (
                                "pause"
                                if node in _PAUSE_NODES
                                else "gate"
                                if node in _GATE_NODES
                                else "model"
                                if node in _MODEL_NODES
                                else "step"
                            ),
                            "state": _snapshot(merged),
                            "actions": [
                                {
                                    "type": a.get("action_type"),
                                    "status": a.get("status"),
                                    "amount": (
                                        Money(int(a["amount_minor"])).format()
                                        if a.get("amount_minor")
                                        else None
                                    ),
                                    "authorisation": a.get("authorisation_decision"),
                                    "policy": a.get("policy_effect"),
                                    "rationale": a.get("rationale"),
                                }
                                for a in merged.get("actions", [])
                            ],
                        }
                    ),
                }

    async for event in pump(graph.astream(seed, config, stream_mode="updates")):
        yield event

    # A case can pause more than once. Resume until it finishes, announcing each
    # pause so the interrupt is visible rather than inferred from a gap.
    while resumes < 6:
        snapshot = await graph.aget_state(config)
        if not snapshot.next:
            break
        resumes += 1
        yield {
            "event": "paused",
            "data": json.dumps(
                {
                    "waiting_on": list(snapshot.next),
                    "note": (
                        "The checkpoint is committed. The process could be killed here and "
                        "this case would resume from exactly this point."
                    ),
                }
            ),
        }
        await asyncio.sleep(STEP_DELAY_SECONDS * 2)
        resume: dict[str, Any] = {
            "decision": "approve",
            "decided_by": "journey@demo",
            "succeeded": True,
        }
        async for event in pump(
            graph.astream(Command(resume=resume), config, stream_mode="updates")
        ):
            yield event

    yield {
        "event": "done",
        "data": json.dumps(
            {
                "status": merged.get("status"),
                "closure_reason": merged.get("closure_reason"),
                "state": _snapshot(merged),
            }
        ),
    }


@router.get("/stream")
async def stream(
    scenario: str = Query(default="fast-retry", description="Which scenario to watch."),
    use_claude: bool = Query(default=False, description="Use real Claude instead of simulator"),
) -> EventSourceResponse:
    """Stream one case through the graph, one node at a time.

    Server-sent events rather than a websocket: the traffic is one-directional
    and SSE reconnects on its own, which is the whole feature set needed here.
    """
    chosen = _BY_KEY.get(scenario, SCENARIOS[0])
    return EventSourceResponse(_events(chosen, use_claude=use_claude))


def now_ist() -> str:
    return to_ist(dt.datetime.now(dt.UTC)).strftime("%H:%M:%S IST")
