"""The API's in-process world.

Anvil's console runs with no database and no credentials. That is a deliberate
choice rather than a shortcut: a reviewer should be able to clone the repository
and see the whole system working in one command, and every step between
``uvicorn`` and a working screen is a step where a demo dies.

So this module holds a seeded :class:`~anvil.simulator.world.World` and, more
interestingly, a set of **genuinely paused LangGraph threads**. The approval
queue is not a list of mock rows: each item is a real graph that has executed
its way to an ``interrupt`` and is sitting on a committed checkpoint waiting for
a human. Approving one from the browser resumes that thread, which then runs
authorisation, policy and the executor for real.

Where a database is configured the same endpoints would read persisted cases
instead. The console does not know the difference, because it only ever sees the
response models in :mod:`anvil.api.schemas`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from anvil.core.clock import FrozenClock
from anvil.core.config import get_settings
from anvil.core.errors import NotFound, OptimisticLockConflict
from anvil.core.logging import get_logger
from anvil.domain.enums import ActionType, ExperimentArm
from anvil.graph.build import build_graph
from anvil.graph.deps import Deps
from anvil.graph.state import initial_state
from anvil.simulator import world as simworld
from anvil.simulator.population import Population, build_population
from anvil.simulator.world import SimCase, World

#: The console's reference instant, so a session in December looks like one in
#: September. Reproducibility is a property of the demo, not just the batch.
CONSOLE_EPOCH = dt.datetime(2026, 9, 1, 6, 0, tzinfo=dt.UTC)

#: Cases held in the live approval queue. Small on purpose -- the queue is there
#: to be worked, and a reviewer looking at three hundred items learns nothing a
#: reviewer looking at eight does not.
LIVE_QUEUE_SIZE = 8

_log = get_logger(__name__)


@dataclass(slots=True)
class LiveCase:
    """One case whose graph is paused, mid-flight, on a committed checkpoint."""

    case: SimCase
    graph: Any
    config: dict[str, Any]
    state: dict[str, Any]
    interrupt: dict[str, Any]
    ledger: simworld._Recorder
    version: int = 1
    resolved: bool = False
    outcome_note: str | None = None

    @property
    def case_id(self) -> str:
        return self.case.case_id

    @property
    def approval_id(self) -> str:
        """Unique per case, always.

        Deliberately derived from the case rather than read from the interrupt
        payload: the batch's approvals port returns a constant id, so trusting
        the payload would collide every approval-kind pause onto one queue entry
        and let a decision on one case resolve another.
        """
        return f"apr_{self.case_id[-10:]}"


@dataclass
class ConsoleState:
    """Everything the API serves, built once and cached."""

    seed: int
    size: int
    population: Population
    world: World
    cases: list[SimCase]
    outcomes: dict[str, Any] = field(default_factory=dict)
    live: dict[str, LiveCase] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def case_by_id(self, case_id: str) -> SimCase:
        for case in self.cases:
            if case.case_id == case_id:
                return case
        raise NotFound(f"no case {case_id}", case_id=case_id)


_state: ConsoleState | None = None


def build_state(*, seed: int, size: int) -> ConsoleState:
    """Construct the world the console serves. Pure apart from the clock epoch."""
    population = build_population(seed=seed, size=size, now=CONSOLE_EPOCH)
    world = World(population)
    return ConsoleState(
        seed=seed,
        size=size,
        population=population,
        world=world,
        cases=world.open_cases(),
    )


def get_state() -> ConsoleState:
    if _state is None:
        raise RuntimeError("console state was never initialised")
    return _state


async def initialise(*, seed: int | None = None, size: int = 900) -> ConsoleState:
    """Build the world and fill the live approval queue. Called on startup."""
    global _state
    settings = get_settings()
    _state = build_state(seed=seed if seed is not None else settings.seed, size=size)
    await _fill_live_queue(_state)
    return _state


def reset() -> None:
    global _state
    _state = None


# ---------------------------------------------------------------------------
# The live, genuinely-paused queue
# ---------------------------------------------------------------------------


def _deps_for(state: ConsoleState, case: SimCase, clock: FrozenClock) -> tuple[Deps, Any]:
    """Wire the graph for a live console case.

    Two differences from the batch: the merchant is left in **review-first**
    mode, so every money-moving action escalates and the queue actually fills;
    and the approvals port is a no-op that returns an id, because the pause
    itself is what the console resumes.
    """
    outcome = state.world._blank_outcome(case)
    recorder = simworld._Recorder(outcome)
    deps = Deps(
        clock=clock,
        classifier=simworld._Classifier(),
        scheduler=simworld._Scheduler(clock),
        scoring=simworld._Scoring(),
        model=simworld._ClassifyingModel(case, state.seed),
        authorisation=simworld._Authorisation(case),
        policy=simworld._Policy(case),
        approvals=simworld._AutoApproval(),
        ledger=recorder,
        gateway=simworld._Gateway(state.world, case, clock, outcome),
        channels=simworld._Channels(state.world, case, outcome),
        audit=recorder,
        cases=recorder,
        allowed_actions=tuple(a.value for a in ActionType),
    )
    return deps, recorder


def _seed_state(state: ConsoleState, case: SimCase) -> dict[str, Any]:
    return initial_state(
        case_id=case.case_id,
        thread_id=f"console:{case.case_id}",
        merchant_id=state.population.merchant_id,
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
        # Left ON, unlike the batch: this is what makes the queue real.
        merchant_review_first=True,
        customer_tenure_days=case.customer.tenure_days,
        customer_lifetime_value_minor=case.customer.lifetime_value.minor,
        budget_headroom_minor=50_000_00,
        customer_concession_headroom_minor=case.amount.minor // 2,
        preferred_language=case.customer.traits.language,
    )


async def _start_case(state: ConsoleState, case: SimCase) -> LiveCase | None:
    """Run a case until it pauses. Returns None if it finished without pausing."""
    clock = FrozenClock(case.failed_at)
    deps, recorder = _deps_for(state, case, clock)
    graph = build_graph(deps, checkpointer=MemorySaver())
    config: dict[str, Any] = {
        "configurable": {"thread_id": f"console:{case.case_id}"},
        "recursion_limit": 80,
    }
    result = await graph.ainvoke(_seed_state(state, case), config)  # type: ignore[arg-type]
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None
    return LiveCase(
        case=case,
        graph=graph,
        config=config,
        state=result,
        interrupt=dict(interrupts[0].value),
        ledger=recorder,
    )


async def _fill_live_queue(state: ConsoleState) -> None:
    """Start cases until the queue has enough paused ones to be worth working."""
    for case in state.cases:
        if len(state.live) >= LIVE_QUEUE_SIZE:
            break
        if case.arm is ExperimentArm.CONTROL:
            continue
        try:
            live = await _start_case(state, case)
        # Broad on purpose: one case that cannot start must not leave the
        # queue empty and the console looking broken.
        except Exception as exc:
            _log.warning("console_case_failed_to_start", case_id=case.case_id, error=str(exc))
            continue
        if live is not None:
            state.live[live.approval_id] = live


async def resolve_approval(
    state: ConsoleState,
    approval_id: str,
    *,
    decision: str,
    decided_by: str,
    version: int,
    note: str | None = None,
    edited_amount_minor: int | None = None,
) -> LiveCase:
    """Resume a paused graph with a human's decision.

    The version check is the console's half of invariant 8's cousin: two
    operators who open the same item both see version 1, the first to resolve it
    writes version 2, and the second gets a conflict and a refreshed view rather
    than silently overwriting a decision that was already made.
    """
    async with state._lock:
        live = state.live.get(approval_id)
        if live is None:
            raise NotFound(f"no pending approval {approval_id}", approval_id=approval_id)
        if live.resolved:
            raise OptimisticLockConflict(
                "this action has already been resolved by someone else",
                approval_id=approval_id,
            )
        if version != live.version:
            raise OptimisticLockConflict(
                f"you were shown version {version} but the current version is {live.version}; "
                "somebody else acted first",
                approval_id=approval_id,
                shown=version,
                current=live.version,
            )

        resume: dict[str, Any] = {"decision": decision, "decided_by": decided_by}
        if note:
            resume["note"] = note
        if decision == "edit" and edited_amount_minor is not None:
            resume["edited_payload"] = {"amount_minor": edited_amount_minor}
        if live.interrupt.get("kind") == "afa_step_up":
            resume["succeeded"] = decision != "reject"

        live.version += 1
        result = await live.graph.ainvoke(Command(resume=resume), live.config)

        # A case can pause more than once. Keep it in the queue if it does.
        interrupts = result.get("__interrupt__")
        live.state = result
        if interrupts:
            live.interrupt = dict(interrupts[0].value)
        else:
            live.resolved = True
            live.outcome_note = str(result.get("closure_reason") or "")
        return live
