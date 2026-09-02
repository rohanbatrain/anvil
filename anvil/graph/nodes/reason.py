"""The two nodes where a language model genuinely earns its place.

Diagnosis maps a heterogeneous, partly free-text signal bundle onto a structured
hypothesis. Planning chooses a sequence from a **closed** action space under a
live budget. Both are judgement under constraint, which is what models are good
at, and both are wrapped so that a model failure degrades the case rather than
stopping it.

The degradation is not a token gesture. When the model is unavailable the
planner falls back to a rule the deterministic scheduler already computed: retry
if the curve says retry, ask for a new instrument if the failure is terminal for
debit, otherwise escalate to a human. That path is exercised by its own test and
demonstrated in the pitch, because a system whose fallback has never run is a
system with no fallback.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from anvil.core.errors import ModelProposedOutOfBounds
from anvil.core.ids import IdPrefix, new_id
from anvil.core.logging import get_logger
from anvil.domain.enums import ActionType, AuditEventType, FailureClass
from anvil.domain.taxonomy import RETRY_CURVES
from anvil.graph.deps import Deps
from anvil.graph.state import ProposedAction, RecoveryState, note

_log = get_logger(__name__)

#: What the planner may propose. The executor re-checks against this, so a model
#: that invents an action type is refused rather than trusted.
DEFAULT_ALLOWED_ACTIONS: tuple[str, ...] = tuple(a.value for a in ActionType)


async def diagnose(deps: Deps, state: RecoveryState) -> dict[str, Any]:
    """Infer what is actually wrong, beyond the failure class.

    The failure class says the debit bounced for insufficient funds. The
    diagnosis is the more useful question: can this customer pay at all, do they
    intend to, and is this a cash-flow timing problem or the beginning of a
    churn? Those are latent facts the simulator genuinely models and the model
    genuinely has to infer, which is what makes this a real task rather than a
    dressed-up lookup.
    """
    now = deps.clock.now()
    try:
        result = await deps.model.diagnose(context=_diagnosis_context(state))
    except Exception as exc:
        _log.warning("diagnosis_unavailable", case_id=state["case_id"], error=str(exc))
        return {
            "degraded": True,
            "degraded_reason": f"the diagnosis model was unavailable ({type(exc).__name__})",
            "diagnosis": _fallback_diagnosis(state),
            "history": note(
                state,
                "diagnose",
                "model unavailable; continuing on the deterministic posture for this failure class",
                at=now.isoformat(),
            ),
        }

    await deps.audit.record(
        event_type=AuditEventType.DIAGNOSIS_PRODUCED.value,
        actor="model",
        actor_kind="agent",
        summary=str(result.get("root_cause", "diagnosis produced"))[:400],
        detail=result,
        case_id=state["case_id"],
        action_id=None,
        merchant_id=state["merchant_id"],
        thread_id=state["thread_id"],
        at=now,
    )
    return {
        "diagnosis": result,
        "model_cost_minor": deps.model.cost_minor,
        "history": note(
            state,
            "diagnose",
            str(result.get("root_cause", "diagnosis produced"))[:200],
            at=now.isoformat(),
        ),
    }


def _diagnosis_context(state: RecoveryState) -> dict[str, Any]:
    """The first-party facts the model may reason over. Nothing invented.

    Every key here is something Anvil observed in its own tables. The guardrail
    in :mod:`anvil.llm.guardrails` later checks that any customer-facing copy
    asserts only facts drawn from this set, which is how "no fabricated urgency"
    stops being a promise and becomes a check.
    """
    return {
        "failure_class": state.get("failure_class"),
        "raw_failure_code": state.get("raw_failure_code"),
        "raw_failure_description": state.get("raw_failure_description"),
        "bank_narration": state.get("bank_narration"),
        "amount_at_risk_minor": state["amount_at_risk_minor"],
        "subscription_mrr_minor": state["subscription_mrr_minor"],
        "currency": state["currency"],
        "customer_tenure_days": state["customer_tenure_days"],
        "prior_failures": state["prior_failures"],
        "prior_recoveries": state["prior_recoveries"],
        "prior_concession_count": state["prior_concession_count"],
        "attempts_made": state["attempts_made"],
        "contacts_made": state["contacts_made"],
        "recovery_likelihood": state.get("recovery_likelihood"),
        "churn_risk": state.get("churn_risk"),
        "history": [h["summary"] for h in state.get("history", [])][-12:],
    }


def _fallback_diagnosis(state: RecoveryState) -> dict[str, Any]:
    """What Anvil believes when the model cannot be reached.

    Drawn entirely from the taxonomy, so it is never wrong -- merely coarse. The
    ``source`` field is set so nothing downstream, and no operator reading the
    console, mistakes it for a model's reasoning.
    """
    failure_class = FailureClass(state.get("failure_class", FailureClass.UNKNOWN.value))
    curve = RETRY_CURVES[failure_class]
    return {
        "source": "deterministic-fallback",
        "root_cause": curve.rationale,
        "can_pay": failure_class is not FailureClass.ACCOUNT_CLOSED,
        "intends_to_pay": failure_class
        not in (FailureClass.MANDATE_REVOKED, FailureClass.MANDATE_PAUSED),
        "recommended_posture": curve.posture.value,
        "confidence": 0,
    }


async def plan(deps: Deps, state: RecoveryState) -> dict[str, Any]:
    """Choose the next action, from a closed set, inside a live budget.

    The model is handed the allowed action list and the remaining concession
    headroom and asked for a sequence. Whatever it returns is filtered: an
    action outside the closed set is dropped and counted as a model-safety
    event, not silently corrected. Counting those is the point -- a dashboard
    that shows "the model proposed something out of bounds 4 times this batch"
    is far more trustworthy than one that implies it never happens.
    """
    now = deps.clock.now()
    allowed = list(deps.allowed_actions or DEFAULT_ALLOWED_ACTIONS)
    budget = min(state["budget_headroom_minor"], state["customer_concession_headroom_minor"])

    try:
        result = await deps.model.plan(
            context=_diagnosis_context(state) | {"diagnosis": state.get("diagnosis")},
            allowed_actions=allowed,
            budget_minor=budget,
        )
        raw_steps = list(result.get("steps", []))
        strategy = str(result.get("strategy", ""))
        degraded = False
    except Exception as exc:
        _log.warning("planner_unavailable", case_id=state["case_id"], error=str(exc))
        raw_steps = _fallback_plan(deps, state)
        strategy = "deterministic fallback: the planner model was unavailable"
        degraded = True

    accepted: list[ProposedAction] = []
    rejected: list[dict[str, Any]] = []
    for index, step in enumerate(raw_steps):
        action_type = str(step.get("action_type", ""))
        if action_type not in allowed:
            rejected.append({"action_type": action_type, "reason": "outside the closed action set"})
            continue
        amount = step.get("amount_minor")
        if amount is not None and (not isinstance(amount, int) or amount <= 0):
            rejected.append({"action_type": action_type, "reason": "non-positive amount"})
            continue
        if ActionType(action_type).is_concession and amount is None:
            rejected.append({"action_type": action_type, "reason": "concession with no amount"})
            continue

        action: ProposedAction = {
            "action_id": new_id(IdPrefix.ACTION),
            "action_type": action_type,
            "sequence": len(state.get("actions", [])) + index,
            "payload": dict(step.get("payload", {})),
            "rationale": str(step.get("rationale", "")),
            "status": "proposed",
        }
        if amount is not None:
            action["amount_minor"] = int(amount)
        if step.get("confidence") is not None:
            action["model_confidence"] = int(step["confidence"])
        accepted.append(action)

    if rejected:
        await deps.audit.record(
            event_type=AuditEventType.MODEL_SAFETY_EVENT.value,
            actor="model",
            actor_kind="agent",
            summary=f"{len(rejected)} proposed step(s) refused before reaching the executor",
            detail={"rejected": rejected, "allowed": allowed},
            case_id=state["case_id"],
            action_id=None,
            merchant_id=state["merchant_id"],
            thread_id=state["thread_id"],
            at=now,
        )

    if not accepted:
        # Never leave a case with nothing to do. Escalating is always available
        # and is always inside policy, so this cannot itself fail.
        accepted = [
            {
                "action_id": new_id(IdPrefix.ACTION),
                "action_type": ActionType.ESCALATE_TO_HUMAN.value,
                "sequence": len(state.get("actions", [])),
                "payload": {},
                "rationale": (
                    "No proposed step survived the closed-action-space check, so the case "
                    "goes to a person rather than proceeding on a guess."
                ),
                "status": "proposed",
            }
        ]

    await deps.audit.record(
        event_type=AuditEventType.PLAN_PRODUCED.value,
        actor="model" if not degraded else "deterministic-fallback",
        actor_kind="agent" if not degraded else "system",
        summary=f"{len(accepted)} action(s) planned: "
        + ", ".join(a["action_type"] for a in accepted),
        detail={"strategy": strategy, "budget_minor": budget},
        case_id=state["case_id"],
        action_id=None,
        merchant_id=state["merchant_id"],
        thread_id=state["thread_id"],
        at=now,
    )

    update: dict[str, Any] = {
        "actions": [*state.get("actions", []), *accepted],
        "current_action_index": len(state.get("actions", [])),
        "plan_strategy": strategy,
        "status": "planning",
        "model_safety_events": state.get("model_safety_events", 0) + len(rejected),
        "model_cost_minor": deps.model.cost_minor,
        "history": note(
            state,
            "plan",
            f"planned {', '.join(a['action_type'] for a in accepted)}",
            at=now.isoformat(),
        ),
    }
    if degraded:
        update["degraded"] = True
        update["degraded_reason"] = strategy
    return update


def _fallback_plan(deps: Deps, state: RecoveryState) -> list[dict[str, Any]]:
    """The plan Anvil makes with no model at all.

    Deliberately conservative and entirely derived from the retry curve: if the
    curve says this class is worth retrying, retry it at the hour the
    deterministic scheduler already chose; if it is terminal for debit, ask the
    customer to fix the instrument; otherwise hand it to a person. No
    concessions are ever offered on this path, because deciding that a
    concession is worth its cost is exactly the judgement the model was there to
    make.
    """
    failure_class = FailureClass(state.get("failure_class", FailureClass.UNKNOWN.value))
    curve = RETRY_CURVES[failure_class]

    if curve.is_retryable:
        return [
            {
                "action_type": ActionType.RETRY_DEBIT.value,
                "amount_minor": state["amount_at_risk_minor"],
                "rationale": (
                    f"Deterministic fallback: {failure_class.value} is retryable and the "
                    f"scheduler has an hour for it. {curve.rationale}"
                ),
                "payload": {},
            }
        ]
    if failure_class in (FailureClass.INSTRUMENT_EXPIRED, FailureClass.MANDATE_REVOKED):
        return [
            {
                "action_type": (
                    ActionType.REQUEST_INSTRUMENT_UPDATE.value
                    if failure_class is FailureClass.INSTRUMENT_EXPIRED
                    else ActionType.REQUEST_MANDATE_REAUTH.value
                ),
                "rationale": (
                    f"Deterministic fallback: {failure_class.value} cannot be retried, so the "
                    "only recovery path is asking the customer to repair the underlying problem."
                ),
                "payload": {},
            }
        ]
    return [
        {
            "action_type": ActionType.ESCALATE_TO_HUMAN.value,
            "rationale": (
                f"Deterministic fallback: {failure_class.value} has no safe unattended action."
            ),
            "payload": {},
        }
    ]


def refuse_out_of_bounds(action_type: str, allowed: list[str]) -> None:
    """Raise when an action reaches the executor that never passed the planner filter.

    A second line of defence. The planner already filters, so reaching this is a
    bug rather than a model failure -- and it should stop the case loudly rather
    than execute something nobody authorised.
    """
    if action_type not in allowed:
        raise ModelProposedOutOfBounds(
            f"{action_type!r} is not in the closed action space",
            action_type=action_type,
            allowed=allowed,
        )


def iso(value: dt.datetime) -> str:
    return value.isoformat()
