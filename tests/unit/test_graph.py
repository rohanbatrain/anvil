"""End-to-end tests of the recovery graph, over hand-written doubles.

Every port is stubbed, which is the point of :mod:`anvil.graph.ports`: the whole
orchestration can be exercised with no database, no network and no model, and
the failure paths can be produced on demand rather than waited for.

The tests that matter most are the ones where something goes wrong. A graph that
recovers a payment on the happy path is unremarkable; a graph that keeps working
when the model is unavailable, refuses what the model should not have proposed,
and declines to guess when the gateway times out is the actual submission.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from anvil.core.clock import FrozenClock
from anvil.domain.enums import (
    ActionType,
    AuthorisationDecision,
    CaseStatus,
    FailureClass,
    PolicyEffect,
)
from anvil.graph.build import build_graph
from anvil.graph.deps import Deps
from anvil.graph.nodes.close import decide_closure
from anvil.graph.state import initial_state
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

NOW = dt.datetime(2026, 9, 18, 6, 0, tzinfo=dt.UTC)
AMOUNT = 1_499_00


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class StubClassifier:
    def __init__(self, resolved: bool = True, failure_class: str = "issuer_technical") -> None:
        self.resolved = resolved
        self.failure_class = failure_class

    def classify(self, **_: Any) -> dict[str, Any]:
        if self.resolved:
            return {
                "resolved": True,
                "failure_class": self.failure_class,
                "confidence_bps": 9000,
                "matched_code": "U30",
            }
        return {"resolved": False, "reason": "no_recognised_signal", "candidates": []}


class StubScheduler:
    def __init__(self, should_retry: bool = True) -> None:
        self.should_retry = should_retry
        self.calls = 0

    def schedule(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if not self.should_retry:
            return {"should_retry": False, "refusal_reason": "this class is never worth retrying"}
        return {
            "should_retry": True,
            "at": (kwargs["now"] + dt.timedelta(hours=6)).isoformat(),
            "probability_bps": 6200,
            "remaining_value_minor": 120_000,
            "explanation": "17:00 IST is clear of the overnight issuer maintenance window",
        }


class StubScoring:
    def score(self, **_: Any) -> dict[str, int]:
        return {"recovery_likelihood": 620, "churn_risk": 180, "priority": 340}


class StubModel:
    """A model that can be made to fail, or to propose something it should not."""

    def __init__(
        self,
        *,
        available: bool = True,
        steps: list[dict[str, Any]] | None = None,
    ) -> None:
        self.available = available
        self.steps = steps
        self.cost = 0
        self.plan_calls = 0

    def _guard(self) -> None:
        if not self.available:
            raise RuntimeError("anthropic api unavailable")

    async def diagnose(self, *, context: dict[str, Any]) -> dict[str, Any]:
        self._guard()
        self.cost += 40
        return {
            "root_cause": "the issuer declined for a transient technical reason",
            "can_pay": True,
            "intends_to_pay": True,
            "confidence": 80,
        }

    async def plan(self, **_: Any) -> dict[str, Any]:
        self._guard()
        self.plan_calls += 1
        self.cost += 120
        steps = (
            self.steps
            if self.steps is not None
            else [
                {
                    "action_type": ActionType.RETRY_DEBIT.value,
                    "amount_minor": AMOUNT,
                    "rationale": "a technical decline usually clears on a retry within hours",
                    "confidence": 80,
                }
            ]
        )
        return {"strategy": "retry once, then contact", "steps": steps}

    async def compose(self, **_: Any) -> dict[str, Any]:
        self._guard()
        self.cost += 30
        return {"subject": "Your payment did not go through", "body": "Please update your details."}

    @property
    def cost_minor(self) -> int:
        return self.cost


class StubAuthorisation:
    def __init__(self, decision: str = AuthorisationDecision.AUTHORISED.value) -> None:
        self.decision = decision
        self.step_ups = 0

    async def authorise(self, **_: Any) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "authorisation_id": "aut_1",
            "attempts_remaining": 3,
            "valid_until": (NOW + dt.timedelta(days=90)).isoformat(),
            "explanation": "within the mandate's per-debit ceiling",
        }

    async def create_step_up(self, **_: Any) -> str:
        self.step_ups += 1
        return "stp_1"


class StubPolicy:
    def __init__(self, effect: str = PolicyEffect.ALLOW.value) -> None:
        self.effect = effect
        self.calls = 0

    async def evaluate(self, **_: Any) -> dict[str, Any]:
        self.calls += 1
        return {
            "effect": self.effect,
            "bundle_id": "pol_1",
            "rule_id": "prl_1",
            "rule_name": "permit-authorised-debit-retries",
            "reason": "permitted",
            "capped_amount_minor": None,
        }


class StubApprovals:
    def __init__(self) -> None:
        self.requested: list[dict[str, Any]] = []

    async def request(self, **kwargs: Any) -> str:
        self.requested.append(kwargs)
        return f"apr_{len(self.requested)}"


class StubLedger:
    def __init__(self, budget_available: bool = True) -> None:
        self.postings: list[tuple[str, int]] = []
        self.budget_available = budget_available

    async def recognise_receivable(self, *, amount_minor: int, **_: Any) -> None:
        self.postings.append(("recognise", amount_minor))

    async def settle_recovered(self, *, amount_minor: int, **_: Any) -> None:
        self.postings.append(("settle", amount_minor))

    async def grant_concession(self, *, amount_minor: int, **_: Any) -> None:
        self.postings.append(("concession", amount_minor))

    async def write_off(self, *, amount_minor: int, **_: Any) -> None:
        self.postings.append(("write_off", amount_minor))

    async def reserve_concession(self, *, amount_minor: int, **_: Any) -> str:
        if not self.budget_available:
            from anvil.core.errors import BudgetExhausted

            raise BudgetExhausted("no headroom left in the authorised budget")
        self.postings.append(("reserve", amount_minor))
        return "rsv_1"

    async def release_concession(self, **_: Any) -> None:
        self.postings.append(("release", 0))

    async def settle_concession(self, **_: Any) -> None:
        self.postings.append(("settle_reservation", 0))


class StubGateway:
    def __init__(self, outcome: str = "settled") -> None:
        self.outcome = outcome
        self.keys: list[str] = []

    async def attempt_debit(self, *, idempotency_key: str, **_: Any) -> dict[str, Any]:
        self.keys.append(idempotency_key)
        if self.outcome == "settled":
            return {"outcome": "settled", "payment_id": "pay_1"}
        if self.outcome == "unknown":
            return {"outcome": "unknown", "detail": "gateway timeout"}
        return {"outcome": "failed", "failure_code": "Z9", "failure_description": "low balance"}

    async def create_payment_link(self, **_: Any) -> dict[str, Any]:
        return {"short_url": "https://rzp.io/l/test"}


class StubChannels:
    def __init__(self, sent: bool = True) -> None:
        self.sent = sent
        self.dispatched: list[dict[str, Any]] = []

    async def dispatch(self, **kwargs: Any) -> dict[str, Any]:
        self.dispatched.append(kwargs)
        if self.sent:
            return {"status": "sent", "sent": True, "cost_minor": 25, "reason": None}
        return {
            "status": "suppressed_frequency_cap",
            "sent": False,
            "cost_minor": 0,
            "reason": "already contacted in the last 24 hours",
        }


class StubAudit:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> None:
        self.records.append(kwargs)

    def types(self) -> list[str]:
        return [r["event_type"] for r in self.records]


class StubCases:
    def __init__(self) -> None:
        self.synced: list[dict[str, Any]] = []

    async def sync(self, state: dict[str, Any], **_: Any) -> None:
        self.synced.append(dict(state))

    async def persist_action(self, **_: Any) -> None:
        return None


def make_deps(**overrides: Any) -> Deps:
    base: dict[str, Any] = {
        "clock": FrozenClock(NOW),
        "classifier": StubClassifier(),
        "scheduler": StubScheduler(),
        "scoring": StubScoring(),
        "model": StubModel(),
        "authorisation": StubAuthorisation(),
        "policy": StubPolicy(),
        "approvals": StubApprovals(),
        "ledger": StubLedger(),
        "gateway": StubGateway(),
        "channels": StubChannels(),
        "audit": StubAudit(),
        "cases": StubCases(),
        "allowed_actions": tuple(a.value for a in ActionType),
    }
    base.update(overrides)
    return Deps(**base)


def make_state(**overrides: Any) -> Any:
    state = initial_state(
        case_id="cse_test",
        thread_id="thread_test",
        merchant_id="mch_test",
        customer_id="cus_test",
        subscription_id="sub_test",
        amount_at_risk_minor=AMOUNT,
        subscription_mrr_minor=AMOUNT,
        original_failure_at=NOW.isoformat(),
        correlation_id="corr_test",
        raw_failure_code="U30",
        consent_state="granted",
        merchant_review_first=False,
        budget_headroom_minor=100_000_00,
        customer_concession_headroom_minor=1_000_00,
    )
    state.update(overrides)
    return state


async def run(deps: Deps, state: Any, thread: str = "t1") -> dict[str, Any]:
    graph = build_graph(deps, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": thread}, "recursion_limit": 60}
    return await graph.ainvoke(state, config)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_a_technical_decline_is_retried_and_recovered() -> None:
    ledger = StubLedger()
    deps = make_deps(ledger=ledger, gateway=StubGateway("settled"))
    final = await run(deps, make_state())

    assert final["status"] == CaseStatus.RECOVERED.value
    assert final["amount_recovered_minor"] == AMOUNT
    assert ("settle", AMOUNT) in ledger.postings
    assert ("recognise", AMOUNT) in ledger.postings


async def test_the_receivable_is_recognised_before_anything_is_recovered() -> None:
    """Order matters: a write-off later must reduce a real asset."""
    ledger = StubLedger()
    await run(make_deps(ledger=ledger), make_state())
    assert ledger.postings[0][0] == "recognise"


async def test_a_recovered_case_writes_nothing_off() -> None:
    ledger = StubLedger()
    await run(make_deps(ledger=ledger), make_state())
    assert not any(kind == "write_off" for kind, _ in ledger.postings)


# ---------------------------------------------------------------------------
# Authorisation and policy are never bypassed
# ---------------------------------------------------------------------------


async def test_every_executed_action_passed_authorisation_and_policy() -> None:
    """Invariants 6 and 7, checked structurally against the audit trail."""
    audit = StubAudit()
    await run(make_deps(audit=audit), make_state())
    types = audit.types()
    assert "authorisation_checked" in types
    assert "policy_evaluated" in types
    assert types.index("authorisation_checked") < types.index("action_executed")
    assert types.index("policy_evaluated") < types.index("action_executed")


async def test_a_policy_denial_stops_the_action_executing() -> None:
    gateway = StubGateway("settled")
    deps = make_deps(policy=StubPolicy(PolicyEffect.DENY.value), gateway=gateway)
    final = await run(deps, make_state())
    assert gateway.keys == []
    assert final["status"] in {
        CaseStatus.ABANDONED.value,
        CaseStatus.UNRECOVERABLE.value,
        CaseStatus.CHURNED.value,
    }


async def test_an_authorisation_denial_stops_the_action_executing() -> None:
    gateway = StubGateway("settled")
    deps = make_deps(
        authorisation=StubAuthorisation(AuthorisationDecision.DENIED.value),
        policy=StubPolicy(PolicyEffect.DENY.value),
        gateway=gateway,
    )
    await run(deps, make_state())
    assert gateway.keys == []


# ---------------------------------------------------------------------------
# The model is bounded
# ---------------------------------------------------------------------------


async def test_an_out_of_bounds_proposal_is_refused_and_counted() -> None:
    """The model asks for something outside the closed set. It does not happen."""
    model = StubModel(
        steps=[
            {
                "action_type": "wire_the_customer_money",
                "amount_minor": 500_00,
                "rationale": "invented",
            },
            {
                "action_type": ActionType.RETRY_DEBIT.value,
                "amount_minor": AMOUNT,
                "rationale": "legitimate",
            },
        ]
    )
    audit = StubAudit()
    final = await run(make_deps(model=model, audit=audit), make_state())

    assert final["model_safety_events"] >= 1
    assert "model_safety_event" in audit.types()
    executed = [a["action_type"] for a in final["actions"] if a.get("status") == "succeeded"]
    assert "wire_the_customer_money" not in executed


async def test_a_concession_with_no_amount_is_refused() -> None:
    model = StubModel(
        steps=[{"action_type": ActionType.OFFER_WINBACK_DISCOUNT.value, "rationale": "no amount"}]
    )
    final = await run(make_deps(model=model), make_state())
    assert final["model_safety_events"] >= 1


async def test_a_negative_amount_is_refused() -> None:
    model = StubModel(
        steps=[
            {
                "action_type": ActionType.RETRY_DEBIT.value,
                "amount_minor": -100,
                "rationale": "negative",
            }
        ]
    )
    final = await run(make_deps(model=model), make_state())
    assert final["model_safety_events"] >= 1


async def test_a_case_with_no_usable_plan_escalates_rather_than_stalling() -> None:
    model = StubModel(steps=[{"action_type": "nonsense", "rationale": "x"}])
    final = await run(make_deps(model=model), make_state())
    proposed = [a["action_type"] for a in final["actions"]]
    assert ActionType.ESCALATE_TO_HUMAN.value in proposed


# ---------------------------------------------------------------------------
# Degradation: the model fails and recovery continues
# ---------------------------------------------------------------------------


async def test_recovery_continues_when_the_model_is_unavailable() -> None:
    """The path the pitch video demonstrates. It must genuinely work."""
    ledger = StubLedger()
    deps = make_deps(
        model=StubModel(available=False), ledger=ledger, gateway=StubGateway("settled")
    )
    final = await run(deps, make_state())

    assert final["degraded"] is True
    assert final["degraded_reason"]
    assert final["status"] == CaseStatus.RECOVERED.value
    assert ("settle", AMOUNT) in ledger.postings


async def test_the_fallback_plan_never_offers_a_concession() -> None:
    """Deciding a concession is worth its cost is exactly what the model was for."""
    deps = make_deps(model=StubModel(available=False), gateway=StubGateway("failed"))
    final = await run(deps, make_state())
    proposed = {a["action_type"] for a in final["actions"]}
    assert not any(ActionType(a).is_concession for a in proposed if a in set(ActionType))


async def test_an_unavailable_classifier_model_falls_back_to_unknown() -> None:
    deps = make_deps(
        classifier=StubClassifier(resolved=False),
        model=StubModel(available=False),
    )
    final = await run(deps, make_state())
    assert final["failure_class"] == FailureClass.UNKNOWN.value
    assert final["degraded"] is True


async def test_a_deterministically_classified_case_never_calls_the_model_to_classify() -> None:
    """The measured claim behind 'the LLM only handles what rules cannot'."""
    deps = make_deps(classifier=StubClassifier(resolved=True))
    final = await run(deps, make_state())
    assert final["classified_deterministically"] is True


# ---------------------------------------------------------------------------
# The unknown gateway outcome
# ---------------------------------------------------------------------------


async def test_a_gateway_timeout_records_nothing_and_asks_for_reconciliation() -> None:
    """Recording a recovery we cannot confirm is worse than recording nothing."""
    ledger = StubLedger()
    deps = make_deps(gateway=StubGateway("unknown"), ledger=ledger)
    final = await run(deps, make_state())

    assert not any(kind == "settle" for kind, _ in ledger.postings)
    assert final["amount_recovered_minor"] == 0
    assert any(a.get("status") == "unknown_outcome" for a in final["actions"])


async def test_the_debit_idempotency_key_is_stable_for_one_logical_action() -> None:
    gateway = StubGateway("failed")
    await run(make_deps(gateway=gateway), make_state())
    assert gateway.keys
    assert all(k.startswith("anvil_") for k in gateway.keys)
    assert len(set(gateway.keys)) == len(gateway.keys), "each action must have its own key"


# ---------------------------------------------------------------------------
# Human in the loop
# ---------------------------------------------------------------------------


async def test_approval_pauses_the_graph_and_resumes_on_approve() -> None:
    """A real interrupt: the graph stops, the checkpoint holds, a person decides."""
    approvals = StubApprovals()
    gateway = StubGateway("settled")
    deps = make_deps(
        policy=StubPolicy(PolicyEffect.REQUIRE_APPROVAL.value),
        approvals=approvals,
        gateway=gateway,
    )
    graph = build_graph(deps, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "t_approval"}, "recursion_limit": 60}

    paused = await graph.ainvoke(make_state(), config)  # type: ignore[arg-type]
    assert "__interrupt__" in paused
    assert approvals.requested, "the queue item must exist before the pause"
    assert gateway.keys == [], "nothing may execute while a human is deciding"

    payload = paused["__interrupt__"][0].value
    assert payload["kind"] == "human_approval"
    assert payload["rationale"], "the operator must see the model's own reasoning"

    final = await graph.ainvoke(
        Command(resume={"decision": "approve", "decided_by": "ops@merchant.example"}),
        config,  # type: ignore[arg-type]
    )
    assert final["status"] == CaseStatus.RECOVERED.value
    assert gateway.keys, "the approved action must actually execute"


async def test_rejecting_an_action_stops_it_executing() -> None:
    gateway = StubGateway("settled")
    deps = make_deps(policy=StubPolicy(PolicyEffect.REQUIRE_APPROVAL.value), gateway=gateway)
    graph = build_graph(deps, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "t_reject"}, "recursion_limit": 60}

    await graph.ainvoke(make_state(), config)  # type: ignore[arg-type]
    final = await graph.ainvoke(
        Command(
            resume={
                "decision": "reject",
                "decided_by": "ops@merchant.example",
                "note": "customer already called in",
            }
        ),
        config,  # type: ignore[arg-type]
    )
    assert gateway.keys == []
    assert final["status"] != CaseStatus.RECOVERED.value


async def test_an_operator_edit_is_what_actually_executes() -> None:
    """An edit must amend the action, not merely suggest an amendment."""
    gateway = StubGateway("settled")
    deps = make_deps(policy=StubPolicy(PolicyEffect.REQUIRE_APPROVAL.value), gateway=gateway)
    graph = build_graph(deps, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "t_edit"}, "recursion_limit": 60}

    await graph.ainvoke(make_state(), config)  # type: ignore[arg-type]
    final = await graph.ainvoke(
        Command(
            resume={
                "decision": "edit",
                "decided_by": "ops@merchant.example",
                "edited_payload": {"amount_minor": 999_00},
            }
        ),
        config,  # type: ignore[arg-type]
    )
    executed = [a for a in final["actions"] if a.get("status") == "succeeded"]
    assert executed
    assert executed[0]["amount_minor"] == 999_00


async def test_step_up_pauses_and_resumes_on_success() -> None:
    """The RBI additional-factor requirement, modelled as a real pause."""
    authorisation = StubAuthorisation(AuthorisationDecision.REQUIRES_STEP_UP.value)
    gateway = StubGateway("settled")
    deps = make_deps(authorisation=authorisation, gateway=gateway)
    graph = build_graph(deps, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "t_stepup"}, "recursion_limit": 60}

    paused = await graph.ainvoke(make_state(), config)  # type: ignore[arg-type]
    assert "__interrupt__" in paused
    assert paused["__interrupt__"][0].value["kind"] == "afa_step_up"
    assert authorisation.step_ups == 1
    assert gateway.keys == []

    final = await graph.ainvoke(Command(resume={"succeeded": True}), config)  # type: ignore[arg-type]
    assert final["status"] == CaseStatus.RECOVERED.value


async def test_a_failed_step_up_does_not_execute() -> None:
    gateway = StubGateway("settled")
    deps = make_deps(
        authorisation=StubAuthorisation(AuthorisationDecision.REQUIRES_STEP_UP.value),
        gateway=gateway,
    )
    graph = build_graph(deps, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "t_stepup_fail"}, "recursion_limit": 60}
    await graph.ainvoke(make_state(), config)  # type: ignore[arg-type]
    final = await graph.ainvoke(Command(resume={"succeeded": False}), config)  # type: ignore[arg-type]
    assert gateway.keys == []
    assert final["status"] != CaseStatus.RECOVERED.value


# ---------------------------------------------------------------------------
# Stopping
# ---------------------------------------------------------------------------


async def test_a_case_that_cannot_settle_is_abandoned_and_written_off() -> None:
    """Abandoning is a decision, and the ledger must reflect it."""
    ledger = StubLedger()
    deps = make_deps(gateway=StubGateway("failed"), ledger=ledger, channels=StubChannels(sent=True))
    final = await run(deps, make_state())

    assert final["status"] in {CaseStatus.ABANDONED.value, CaseStatus.UNRECOVERABLE.value}
    assert any(kind == "write_off" for kind, _ in ledger.postings)
    assert final["closure_reason"]


async def test_the_graph_always_reaches_a_resting_state() -> None:
    """Whatever the world does, the graph stops somewhere defensible.

    That means either a terminal status, or PENDING_RECONCILIATION -- which is
    deliberately non-terminal, because a case whose last attempt returned no
    answer has not finished, it is waiting on the gateway. Never an open loop
    and never a status nobody chose.
    """
    resting = {*[s for s in CaseStatus if s.is_terminal], CaseStatus.PENDING_RECONCILIATION}
    for outcome in ("failed", "unknown", "settled"):
        for sent in (True, False):
            deps = make_deps(gateway=StubGateway(outcome), channels=StubChannels(sent=sent))
            final = await run(deps, make_state(), thread=f"t_{outcome}_{sent}")
            assert CaseStatus(final["status"]) in resting, (outcome, sent, final["status"])


# ---------------------------------------------------------------------------
# Closure classification
# ---------------------------------------------------------------------------


def test_a_revoked_mandate_is_labelled_churn_not_failure() -> None:
    """The customer decided. Calling that a payment failure would be dishonest."""
    status, reason = decide_closure(
        {
            "amount_at_risk_minor": AMOUNT,
            "amount_recovered_minor": 0,
            "failure_class": FailureClass.MANDATE_REVOKED.value,
        }  # type: ignore[arg-type]
    )
    assert status is CaseStatus.CHURNED
    assert "decision" in reason


def test_an_expired_card_with_no_recovery_is_unrecoverable_not_churn() -> None:
    status, _ = decide_closure(
        {
            "amount_at_risk_minor": AMOUNT,
            "amount_recovered_minor": 0,
            "failure_class": FailureClass.INSTRUMENT_EXPIRED.value,
        }  # type: ignore[arg-type]
    )
    assert status is CaseStatus.UNRECOVERABLE


def test_a_partial_recovery_counts_as_recovered() -> None:
    status, reason = decide_closure(
        {
            "amount_at_risk_minor": AMOUNT,
            "amount_recovered_minor": 500_00,
            "failure_class": FailureClass.INSUFFICIENT_FUNDS.value,
        }  # type: ignore[arg-type]
    )
    assert status is CaseStatus.RECOVERED
    assert "Partially" in reason


def test_a_recovery_that_cost_a_concession_says_so() -> None:
    _, reason = decide_closure(
        {
            "amount_at_risk_minor": AMOUNT,
            "amount_recovered_minor": AMOUNT,
            "concession_granted_minor": 200_00,
            "failure_class": FailureClass.INSUFFICIENT_FUNDS.value,
        }  # type: ignore[arg-type]
    )
    assert "conceding" in reason


@pytest.mark.parametrize("failure_class", list(FailureClass))
def test_closure_is_total_over_every_failure_class(failure_class: FailureClass) -> None:
    status, reason = decide_closure(
        {
            "amount_at_risk_minor": AMOUNT,
            "amount_recovered_minor": 0,
            "failure_class": failure_class.value,
            "attempts_made": 2,
            "contacts_made": 1,
        }  # type: ignore[arg-type]
    )
    assert status.is_terminal
    assert reason


def test_an_unresolved_attempt_is_parked_not_abandoned() -> None:
    """We do not know whether that debit took the money. Saying so is the only
    honest option, and writing it off would be a claim we cannot support."""
    status, reason = decide_closure(
        {
            "amount_at_risk_minor": AMOUNT,
            "amount_recovered_minor": 0,
            "failure_class": FailureClass.ISSUER_TECHNICAL.value,
            "status": CaseStatus.PENDING_RECONCILIATION.value,
        }  # type: ignore[arg-type]
    )
    assert status is CaseStatus.PENDING_RECONCILIATION
    assert not status.is_terminal
    assert "idempotency key" in reason


async def test_a_gateway_timeout_writes_nothing_off() -> None:
    ledger = StubLedger()
    deps = make_deps(gateway=StubGateway("unknown"), ledger=ledger)
    final = await run(deps, make_state(), thread="t_unknown_writeoff")
    assert final["status"] == CaseStatus.PENDING_RECONCILIATION.value
    assert not any(kind == "write_off" for kind, _ in ledger.postings)
