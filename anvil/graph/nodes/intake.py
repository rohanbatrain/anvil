"""Opening a case: recognise the receivable, classify the failure, gather context.

The classification node is where the architecture's central claim becomes
visible. It tries the deterministic tables first and only escalates to the model
when they genuinely cannot decide -- and when it does escalate, it records that
it did, so ``classified_deterministically`` on the case row is a measured fact
rather than an assumption. The batch report later reports the split, which is
the honest way to answer "how much is the LLM actually doing here?".
"""

from __future__ import annotations

from typing import Any

from anvil.core.logging import get_logger
from anvil.domain.enums import AuditEventType, FailureClass
from anvil.graph.deps import Deps
from anvil.graph.state import RecoveryState, note

_log = get_logger(__name__)


async def ingest(deps: Deps, state: RecoveryState) -> dict[str, Any]:
    """Open the case and put the money on the books.

    Recognising the receivable here rather than at recovery time is what lets a
    later write-off reduce a real asset, and it makes "how much are we chasing
    right now?" a ledger balance instead of a query over case rows.
    """
    now = deps.clock.now()
    await deps.ledger.recognise_receivable(
        case_id=state["case_id"],
        customer_id=state["customer_id"],
        merchant_id=state["merchant_id"],
        amount_minor=state["amount_at_risk_minor"],
        at=now,
    )
    await deps.audit.record(
        event_type=AuditEventType.CASE_OPENED.value,
        actor="agent",
        actor_kind="agent",
        summary=(
            f"Case opened for {state['amount_at_risk_minor'] / 100:,.2f} "
            f"{state['currency']} at risk"
        ),
        detail={"raw_failure_code": state.get("raw_failure_code")},
        case_id=state["case_id"],
        action_id=None,
        merchant_id=state["merchant_id"],
        thread_id=state["thread_id"],
        at=now,
    )
    return {
        "status": "diagnosing",
        "history": note(
            state,
            "ingest",
            f"Case opened; {state['amount_at_risk_minor'] / 100:,.2f} at risk",
            at=now.isoformat(),
        ),
    }


async def classify(deps: Deps, state: RecoveryState) -> dict[str, Any]:
    """Resolve the failure class deterministically, or escalate to the model.

    The escalation is not a fallback for a broken lookup -- it is the designed
    path for the roughly one failure in five that arrives as free text no code
    table has ever seen. When the model is unavailable the node degrades to
    ``UNKNOWN``, whose retry curve permits exactly one conservative attempt
    before a human looks at it. Recovery continues; it just gets less clever.
    """
    now = deps.clock.now()
    verdict = deps.classifier.classify(
        raw_code=state.get("raw_failure_code"),
        gateway_description=state.get("raw_failure_description"),
        bank_narration=state.get("bank_narration"),
        rail_hint=state.get("rail_hint"),
    )

    if verdict.get("resolved"):
        failure_class = str(verdict["failure_class"])
        await deps.audit.record(
            event_type=AuditEventType.FAILURE_CLASSIFIED.value,
            actor="deterministic-classifier",
            actor_kind="system",
            summary=f"Classified as {failure_class} without a model call",
            detail=verdict,
            case_id=state["case_id"],
            action_id=None,
            merchant_id=state["merchant_id"],
            thread_id=state["thread_id"],
            at=now,
        )
        return {
            "failure_class": failure_class,
            "classified_deterministically": True,
            "classification_confidence_bps": int(verdict.get("confidence_bps", 0)),
            "history": note(
                state,
                "classify",
                f"{failure_class} resolved from the code tables ({verdict.get('matched_code')})",
                at=now.isoformat(),
            ),
        }

    try:
        result = await deps.model.diagnose(
            context={"purpose": "classification", **_classification_context(state, verdict)}
        )
        failure_class = str(result.get("failure_class", FailureClass.UNKNOWN.value))
        degraded = False
        reason = None
        confidence = int(result.get("confidence", 0)) * 100
    except Exception as exc:
        _log.warning("classifier_model_unavailable", case_id=state["case_id"], error=str(exc))
        failure_class = FailureClass.UNKNOWN.value
        degraded = True
        reason = f"the classifier model was unavailable ({type(exc).__name__})"
        confidence = 0

    await deps.audit.record(
        event_type=AuditEventType.FAILURE_CLASSIFIED.value,
        actor="model" if not degraded else "deterministic-fallback",
        actor_kind="agent" if not degraded else "system",
        summary=(
            f"Classified as {failure_class} "
            f"{'by the model' if not degraded else 'by fallback after a model failure'}"
        ),
        detail={"escalation_reason": verdict.get("reason"), "degraded": degraded},
        case_id=state["case_id"],
        action_id=None,
        merchant_id=state["merchant_id"],
        thread_id=state["thread_id"],
        at=now,
    )

    update: dict[str, Any] = {
        "failure_class": failure_class,
        "classified_deterministically": False,
        "classification_confidence_bps": confidence,
        "model_cost_minor": deps.model.cost_minor,
        "history": note(
            state,
            "classify",
            f"{failure_class} after escalation ({verdict.get('reason', 'unresolved')})",
            at=now.isoformat(),
        ),
    }
    if degraded:
        update["degraded"] = True
        update["degraded_reason"] = reason
    return update


def _classification_context(state: RecoveryState, verdict: dict[str, Any]) -> dict[str, Any]:
    """Exactly what the classifier model is shown. Nothing more."""
    return {
        "raw_code": state.get("raw_failure_code"),
        "gateway_description": state.get("raw_failure_description"),
        "bank_narration": state.get("bank_narration"),
        "rail_hint": state.get("rail_hint"),
        "candidates": verdict.get("candidates", []),
        "escalation_reason": verdict.get("reason"),
    }


async def score(deps: Deps, state: RecoveryState) -> dict[str, Any]:
    """Attach recovery likelihood, churn risk and priority. No model involved."""
    now = deps.clock.now()
    scheduler_view = deps.scheduler.schedule(
        failure_class=state.get("failure_class", FailureClass.UNKNOWN.value),
        amount_at_risk_minor=state["amount_at_risk_minor"],
        failed_at=_parse(state["original_failure_at"]),
        now=now,
        attempts_used=state["attempts_made"],
        mandate_attempts_remaining=state.get("mandate_attempts_remaining"),
        mandate_valid_until=(
            _parse(state["mandate_valid_until"]) if state.get("mandate_valid_until") else None
        ),
    )
    scores = deps.scoring.score(
        failure_class=state.get("failure_class", FailureClass.UNKNOWN.value),
        amount_at_risk_minor=state["amount_at_risk_minor"],
        tenure_days=state["customer_tenure_days"],
        prior_failures=state["prior_failures"],
        prior_recoveries=state["prior_recoveries"],
        lifetime_value_minor=state["customer_lifetime_value_minor"],
        attempts_used=state["attempts_made"],
        contacts_made=state["contacts_made"],
        scheduler_probability_bps=(
            int(scheduler_view.get("probability_bps", 0))
            if scheduler_view.get("should_retry")
            else None
        ),
    )
    return {
        "recovery_likelihood": scores["recovery_likelihood"],
        "churn_risk": scores["churn_risk"],
        "priority_score": scores["priority"],
        "history": note(
            state,
            "score",
            (
                f"recovery {scores['recovery_likelihood']}/1000, "
                f"churn {scores['churn_risk']}/1000, priority {scores['priority']}/1000"
            ),
            at=now.isoformat(),
        ),
    }


def _parse(value: str):  # type: ignore[no-untyped-def]
    import datetime as dt

    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
