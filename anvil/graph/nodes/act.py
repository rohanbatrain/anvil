"""Scheduling, executing and observing a single approved action.

The executor is the narrowest part of the system on purpose. By the time an
action reaches it, it has an authorisation, a policy pass, an optional human
signature and a stable idempotency key. The executor's job is to perform exactly
that action and to record honestly what happened -- including the case where it
cannot tell.

**The unknown outcome is first-class.** A gateway timeout does not mean failure.
It means the request may or may not have moved money, and the only safe response
is to mark the attempt for reconciliation and stop touching it. Treating a
timeout as a failure and retrying is how a customer gets charged twice, so this
module never does it.
"""

from __future__ import annotations

from typing import Any

from anvil.core.errors import BudgetExhausted
from anvil.core.ids import idempotency_key
from anvil.core.logging import get_logger
from anvil.domain.enums import ActionType, AuditEventType, CaseStatus, FailureClass
from anvil.graph.deps import Deps
from anvil.graph.state import RecoveryState, current_action, note, replace_action

_log = get_logger(__name__)


async def schedule(deps: Deps, state: RecoveryState) -> dict[str, Any]:
    """Pick the hour for a debit retry, using the deterministic optimiser.

    Only retries are scheduled. Outreach and instrument requests go out as soon
    as policy permits, because their value does not depend on issuer timing --
    and the quiet-hours rule already governs when a customer may be contacted.
    """
    now = deps.clock.now()
    action = current_action(state)
    if action is None:
        return {"status": "planning"}
    index = state.get("current_action_index", 0)

    if ActionType(action["action_type"]) not in (ActionType.RETRY_DEBIT, ActionType.SPLIT_DEBIT):
        return {"actions": replace_action(state, index, scheduled_for=now.isoformat())}

    decision = deps.scheduler.schedule(
        failure_class=state.get("failure_class", FailureClass.UNKNOWN.value),
        amount_at_risk_minor=int(action.get("amount_minor", state["amount_at_risk_minor"])),
        failed_at=_parse(state["original_failure_at"]),
        now=now,
        attempts_used=state["attempts_made"],
        mandate_attempts_remaining=state.get("mandate_attempts_remaining"),
        mandate_valid_until=(
            _parse(state["mandate_valid_until"]) if state.get("mandate_valid_until") else None
        ),
    )

    if not decision.get("should_retry"):
        return {
            "actions": replace_action(
                state,
                index,
                status="cancelled",
                outcome={"reason": decision.get("refusal_reason")},
            ),
            "status": "planning",
            "history": note(
                state,
                "schedule",
                f"retry refused: {decision.get('refusal_reason', '')}"[:200],
                at=now.isoformat(),
            ),
        }

    return {
        "actions": replace_action(
            state,
            index,
            scheduled_for=str(decision["at"]),
            expected_probability_bps=int(decision.get("probability_bps", 0)),
            expected_recovery_minor=int(decision.get("remaining_value_minor", 0)),
            status="scheduled",
        ),
        "next_action_at": str(decision["at"]),
        "status": "scheduled",
        "history": note(
            state, "schedule", str(decision.get("explanation", ""))[:250], at=now.isoformat()
        ),
    }


async def execute(deps: Deps, state: RecoveryState) -> dict[str, Any]:
    """Perform the action. One branch per action family, and no other paths."""
    now = deps.clock.now()
    action = current_action(state)
    if action is None:
        return {"status": "planning"}
    index = state.get("current_action_index", 0)
    action_type = ActionType(action["action_type"])

    key = idempotency_key(
        state["case_id"],
        action["action_id"],
        action["action_type"],
        str(action.get("amount_minor", 0)),
    )

    if action_type in (ActionType.RETRY_DEBIT, ActionType.SPLIT_DEBIT):
        return await _execute_debit(deps, state, action, index, key, now)
    if action_type.is_concession:
        return await _execute_concession(deps, state, action, index, key, now)
    if action_type in (
        ActionType.SEND_REMINDER,
        ActionType.SEND_DUNNING_NOTICE,
        ActionType.REQUEST_INSTRUMENT_UPDATE,
        ActionType.REQUEST_MANDATE_REAUTH,
        ActionType.SEND_PAYMENT_LINK,
    ):
        return await _execute_outreach(deps, state, action, index, now)
    return {
        "actions": replace_action(state, index, status="succeeded", idempotency_key=key),
        "status": "closing",
    }


async def _execute_debit(
    deps: Deps,
    state: RecoveryState,
    action: dict[str, Any],
    index: int,
    key: str,
    now: Any,
) -> dict[str, Any]:
    amount = int(action.get("amount_minor", state["amount_at_risk_minor"]))
    result = await deps.gateway.attempt_debit(
        case_id=state["case_id"],
        subscription_id=state["subscription_id"],
        amount_minor=amount,
        idempotency_key=key,
        now=now,
    )
    outcome = str(result.get("outcome", "unknown"))

    await deps.audit.record(
        event_type=AuditEventType.ACTION_EXECUTED.value,
        actor="agent",
        actor_kind="agent",
        summary=f"debit attempt for {amount / 100:,.2f} -> {outcome}",
        detail={"idempotency_key": key, **result},
        case_id=state["case_id"],
        action_id=action["action_id"],
        merchant_id=state["merchant_id"],
        thread_id=state["thread_id"],
        at=now,
    )

    if outcome == "settled":
        await deps.ledger.settle_recovered(
            case_id=state["case_id"],
            action_id=action["action_id"],
            customer_id=state["customer_id"],
            merchant_id=state["merchant_id"],
            amount_minor=amount,
            at=now,
        )
        return {
            "actions": replace_action(
                state, index, status="succeeded", idempotency_key=key, outcome=result
            ),
            "amount_recovered_minor": state["amount_recovered_minor"] + amount,
            "attempts_made": state["attempts_made"] + 1,
            "status": "closing",
            "history": note(state, "execute", f"recovered {amount / 100:,.2f}", at=now.isoformat()),
        }

    if outcome == "unknown":
        # The books stay untouched. Recording a recovery we cannot confirm would
        # be worse than recording nothing, and reconciliation will resolve it.
        return {
            "actions": replace_action(
                state, index, status="unknown_outcome", idempotency_key=key, outcome=result
            ),
            "attempts_made": state["attempts_made"] + 1,
            "status": CaseStatus.PENDING_RECONCILIATION.value,
            "history": note(
                state,
                "execute",
                "gateway outcome unknown; the attempt is queued for reconciliation with the "
                "same idempotency key rather than retried",
                at=now.isoformat(),
            ),
        }

    return {
        "actions": replace_action(
            state, index, status="failed", idempotency_key=key, outcome=result
        ),
        "attempts_made": state["attempts_made"] + 1,
        "raw_failure_code": result.get("failure_code", state.get("raw_failure_code")),
        "raw_failure_description": result.get(
            "failure_description", state.get("raw_failure_description")
        ),
        "status": "observing",
        "history": note(
            state,
            "execute",
            f"debit failed: {result.get('failure_code', 'unknown')}",
            at=now.isoformat(),
        ),
    }


async def _execute_concession(
    deps: Deps,
    state: RecoveryState,
    action: dict[str, Any],
    index: int,
    key: str,
    now: Any,
) -> dict[str, Any]:
    """Reserve, then grant. Never grant without a hold that succeeded.

    The reservation is taken under a row lock inside the ledger, so two cases
    racing for the last of a budget cannot both win. If the hold fails the
    action is abandoned and the planner is re-entered with concessions
    effectively unavailable -- which is the correct behaviour, not an error.
    """
    amount = int(action.get("amount_minor", 0))
    try:
        reservation_id = await deps.ledger.reserve_concession(
            case_id=state["case_id"],
            action_id=action["action_id"],
            customer_id=state["customer_id"],
            merchant_id=state["merchant_id"],
            amount_minor=amount,
            subscription_mrr_minor=state["subscription_mrr_minor"],
            now=now,
        )
    except BudgetExhausted as exc:
        _log.info("concession_refused", case_id=state["case_id"], reason=exc.message)
        return {
            "actions": replace_action(
                state, index, status="denied_by_policy", outcome={"reason": exc.message}
            ),
            "status": "planning",
            "history": note(
                state, "execute", f"concession refused: {exc.message}"[:200], at=now.isoformat()
            ),
        }

    await deps.ledger.grant_concession(
        case_id=state["case_id"],
        action_id=action["action_id"],
        customer_id=state["customer_id"],
        merchant_id=state["merchant_id"],
        amount_minor=amount,
        at=now,
    )
    await deps.ledger.settle_concession(reservation_id=reservation_id, now=now)
    await deps.audit.record(
        event_type=AuditEventType.ACTION_EXECUTED.value,
        actor="agent",
        actor_kind="agent",
        summary=f"concession of {amount / 100:,.2f} granted",
        detail={"reservation_id": reservation_id, "idempotency_key": key},
        case_id=state["case_id"],
        action_id=action["action_id"],
        merchant_id=state["merchant_id"],
        thread_id=state["thread_id"],
        at=now,
    )
    return {
        "actions": replace_action(
            state, index, status="succeeded", reservation_id=reservation_id, idempotency_key=key
        ),
        "concession_granted_minor": state["concession_granted_minor"] + amount,
        "prior_concession_count": state["prior_concession_count"] + 1,
        "status": "planning",
        "history": note(state, "execute", f"conceded {amount / 100:,.2f}", at=now.isoformat()),
    }


async def _execute_outreach(
    deps: Deps, state: RecoveryState, action: dict[str, Any], index: int, now: Any
) -> dict[str, Any]:
    """Compose the message, then hand it to the channel layer, which may refuse.

    Composition degrades to a deterministic template when the model is
    unavailable. The channel layer runs consent, frequency and quiet-hours
    checks of its own -- deliberately duplicating the policy engine, because the
    two protect different things and a send that slipped past one should still
    meet the other.
    """
    purpose = str(action.get("payload", {}).get("purpose", "payment_recovery_outreach"))
    language = state.get("preferred_language", "en")

    try:
        draft = await deps.model.compose(
            context={
                "failure_class": state.get("failure_class"),
                "diagnosis": state.get("diagnosis"),
                "amount_minor": state["amount_at_risk_minor"],
                "currency": state["currency"],
                "action_type": action["action_type"],
            },
            purpose=purpose,
            language=language,
            allowed_facts=[
                "failure_class",
                "amount_minor",
                "currency",
                "subscription_mrr_minor",
                "customer_tenure_days",
            ],
        )
        subject = draft.get("subject")
        body = str(draft.get("body", ""))
        degraded = False
    except Exception as exc:
        _log.warning("composer_unavailable", case_id=state["case_id"], error=str(exc))
        subject, body = _template(state, purpose, language)
        degraded = True

    result = await deps.channels.dispatch(
        case_id=state["case_id"],
        customer_id=state["customer_id"],
        merchant_id=state["merchant_id"],
        purpose=purpose,
        language=language,
        subject=subject,
        body=body,
        now=now,
    )
    sent = bool(result.get("sent"))

    await deps.audit.record(
        event_type=AuditEventType.MESSAGE_DISPATCHED.value,
        actor="agent" if not degraded else "deterministic-template",
        actor_kind="agent" if not degraded else "system",
        summary=(
            f"{purpose} {'sent' if sent else 'suppressed'}"
            + (f": {result.get('reason')}" if not sent else "")
        ),
        detail={"status": result.get("status"), "cost_minor": result.get("cost_minor", 0)},
        case_id=state["case_id"],
        action_id=action["action_id"],
        merchant_id=state["merchant_id"],
        thread_id=state["thread_id"],
        at=now,
    )

    update: dict[str, Any] = {
        "actions": replace_action(
            state, index, status="succeeded" if sent else "cancelled", outcome=result
        ),
        "contacts_made": state["contacts_made"] + (1 if sent else 0),
        "channel_cost_minor": state["channel_cost_minor"] + int(result.get("cost_minor", 0)),
        "status": "observing" if sent else "planning",
        "history": note(
            state,
            "execute",
            f"{purpose} {'sent' if sent else 'suppressed: ' + str(result.get('reason', ''))}"[:200],
            at=now.isoformat(),
        ),
    }
    if degraded:
        update["degraded"] = True
        update["degraded_reason"] = "the composer model was unavailable; a template was used"
    return update


def _template(state: RecoveryState, purpose: str, language: str) -> tuple[str, str]:
    """Deterministic fallback copy. Deliberately plain and factual.

    Asserts only amounts and facts Anvil already holds. No urgency it cannot
    justify, no offer it has not been authorised to make.
    """
    amount = f"{state['amount_at_risk_minor'] / 100:,.2f}"
    if language == "hi":
        return (
            "आपका भुगतान पूरा नहीं हो सका",
            f"आपकी सदस्यता के लिए ₹{amount} का भुगतान पूरा नहीं हो सका। कृपया अपना भुगतान विवरण जांचें।",
        )
    return (
        "We could not collect your subscription payment",
        f"A payment of {amount} for your subscription did not go through. "
        "You can update your payment details or retry from your account.",
    )


async def observe(deps: Deps, state: RecoveryState) -> dict[str, Any]:
    """Decide what the outcome means for the case: continue, or stop.

    The stopping rules live in the policy engine rather than here, so this node
    only advances the cursor and lets the router re-enter planning. That
    separation is what keeps "when do we give up?" a merchant-editable policy
    question rather than a constant buried in orchestration code.
    """
    now = deps.clock.now()
    actions = state.get("actions", [])
    index = state.get("current_action_index", 0)
    next_index = index + 1

    if state["amount_recovered_minor"] >= state["amount_at_risk_minor"]:
        return {"status": "closing", "current_action_index": next_index}

    if next_index < len(actions):
        return {
            "current_action_index": next_index,
            "status": "executing",
            "history": note(
                state, "observe", "moving to the next planned action", at=now.isoformat()
            ),
        }

    return {
        "current_action_index": next_index,
        "status": "planning",
        "history": note(
            state,
            "observe",
            "plan exhausted; re-planning with what we now know",
            at=now.isoformat(),
        ),
    }


def _parse(value: str):  # type: ignore[no-untyped-def]
    import datetime as dt

    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
