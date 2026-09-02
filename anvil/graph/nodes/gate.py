"""The gates every action passes before anything moves.

Four checks in a fixed order, and the order is the design:

1. **Authorisation.** Is there a stored right to do this at all? Structural, and
   it fails closed.
2. **Step-up.** If the action is inside the principal's authority but outside
   the agent's delegated cap, the graph *stops* and asks the customer to
   re-authenticate. A real interrupt, not a simulated one.
3. **Policy.** Is it permitted, and how much of it? Deterministic, and it
   records which rule decided.
4. **Approval.** If policy escalated it, the graph stops and asks a person.

Authorisation comes first because it is the only check that answers "does this
right exist?" rather than "should we exercise it?". Running policy first would
mean writing rules about actions the merchant was never authorised to take,
which invites a bundle that quietly permits something no mandate covers.

Both interrupts are durable. LangGraph writes the checkpoint before the
``interrupt`` call returns control, so the process can be killed at that instant
and the case resumes from exactly here.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from langgraph.types import interrupt

from anvil.core.logging import get_logger
from anvil.domain.enums import (
    ActionType,
    AuditEventType,
    AuthorisationDecision,
    PolicyEffect,
)
from anvil.graph.deps import Deps
from anvil.graph.state import RecoveryState, current_action, note, replace_action

_log = get_logger(__name__)


async def authorise(deps: Deps, state: RecoveryState) -> dict[str, Any]:
    """Check the mandate registry. Fails closed, and records why.

    Actions that move no money still pass through here rather than skipping it,
    because the authorisation result is a *fact the policy engine reads* --
    ``unauthorised-actions-never-execute`` needs a value to test, and an absent
    value would make that immutable rule silently vacuous.
    """
    now = deps.clock.now()
    action = current_action(state)
    if action is None:
        return {"status": "planning"}

    index = state.get("current_action_index", 0)
    action_type = ActionType(action["action_type"])

    if action_type.is_terminal:
        # Stopping never needs a mandate. Requiring one would mean a case with a
        # revoked mandate could not even be closed.
        return {
            "actions": replace_action(
                state, index, authorisation_decision=AuthorisationDecision.AUTHORISED.value
            )
        }

    verdict = await deps.authorisation.authorise(
        customer_id=state["customer_id"],
        subscription_id=state["subscription_id"],
        action_type=action["action_type"],
        amount_minor=int(action.get("amount_minor", 0)),
        now=now,
    )
    decision = str(verdict.get("decision", AuthorisationDecision.DENIED.value))

    await deps.audit.record(
        event_type=AuditEventType.AUTHORISATION_CHECKED.value,
        actor="mandate-registry",
        actor_kind="system",
        summary=f"{action['action_type']} -> {decision}"
        + (f" ({verdict.get('denial_reason')})" if verdict.get("denial_reason") else ""),
        detail=verdict,
        case_id=state["case_id"],
        action_id=action["action_id"],
        merchant_id=state["merchant_id"],
        thread_id=state["thread_id"],
        at=now,
    )

    updates: dict[str, Any] = {
        "authorisation_decision": decision,
        "authorisation_id": verdict.get("authorisation_id"),
    }
    if verdict.get("denial_reason"):
        updates["denial_reason"] = str(verdict["denial_reason"])
    if decision == AuthorisationDecision.DENIED.value:
        updates["status"] = "denied_by_authorisation"

    return {
        "actions": replace_action(state, index, **updates),
        "authorisation_id": verdict.get("authorisation_id"),
        "mandate_attempts_remaining": verdict.get("attempts_remaining"),
        "mandate_valid_until": verdict.get("valid_until"),
        "history": note(
            state,
            "authorise",
            f"{action['action_type']} {decision}"
            + (f": {verdict.get('explanation', '')}" if verdict.get("explanation") else ""),
            at=now.isoformat(),
        ),
    }


async def step_up(deps: Deps, state: RecoveryState) -> dict[str, Any]:
    """Pause the case until the customer re-authenticates.

    This is the RBI additional-factor requirement modelled honestly rather than
    assumed away. The graph genuinely stops here: the challenge is created, the
    checkpoint is committed, and the node yields. Whatever resumes it -- an OTP
    submitted hours later, on a different process -- arrives as the value of the
    ``interrupt`` call.
    """
    now = deps.clock.now()
    action = current_action(state)
    if action is None:
        return {"status": "planning"}
    index = state.get("current_action_index", 0)

    challenge_id = await deps.authorisation.create_step_up(
        case_id=state["case_id"],
        action_id=action["action_id"],
        authorisation_id=str(action.get("authorisation_id") or state.get("authorisation_id") or ""),
        customer_id=state["customer_id"],
        amount_minor=int(action.get("amount_minor", 0)),
        thread_id=state["thread_id"],
        now=now,
    )
    await deps.audit.record(
        event_type=AuditEventType.STEP_UP_REQUESTED.value,
        actor="agent",
        actor_kind="agent",
        summary=(
            f"{action['action_type']} is within the principal's authority but exceeds the "
            "agent's delegated cap, so the customer must re-authenticate"
        ),
        detail={"challenge_id": challenge_id},
        case_id=state["case_id"],
        action_id=action["action_id"],
        merchant_id=state["merchant_id"],
        thread_id=state["thread_id"],
        at=now,
    )

    # Execution stops here. The checkpoint is durable; the value below is
    # whatever Command(resume=...) supplies whenever the customer responds.
    result: dict[str, Any] = interrupt(
        {
            "kind": "afa_step_up",
            "challenge_id": challenge_id,
            "case_id": state["case_id"],
            "action_id": action["action_id"],
            "amount_minor": action.get("amount_minor"),
            "reason": "the action exceeds the delegated agent cap",
        }
    )

    resumed_at = deps.clock.now()
    succeeded = bool(result.get("succeeded"))
    await deps.audit.record(
        event_type=AuditEventType.STEP_UP_RESOLVED.value,
        actor="customer",
        actor_kind="customer",
        summary=f"step-up {'succeeded' if succeeded else 'failed'}",
        detail={"challenge_id": challenge_id},
        case_id=state["case_id"],
        action_id=action["action_id"],
        merchant_id=state["merchant_id"],
        thread_id=state["thread_id"],
        at=resumed_at,
    )

    return {
        "step_up_result": result,
        "pending_step_up_id": None,
        "actions": replace_action(
            state,
            index,
            authorisation_decision=(
                AuthorisationDecision.AUTHORISED.value
                if succeeded
                else AuthorisationDecision.DENIED.value
            ),
            denial_reason=None if succeeded else "step_up_failed",
        ),
        "status": "executing" if succeeded else "denied_by_authorisation",
        "history": note(
            state,
            "step_up",
            f"customer re-authentication {'succeeded' if succeeded else 'failed'}",
            at=resumed_at.isoformat(),
        ),
    }


async def policy(deps: Deps, state: RecoveryState) -> dict[str, Any]:
    """Evaluate the active bundle and record which rule decided.

    Invariant 7: an action does not execute without one of these, and the
    persisted evaluation names the exact bundle and rule that permitted it.
    """
    now = deps.clock.now()
    action = current_action(state)
    if action is None:
        return {"status": "planning"}
    index = state.get("current_action_index", 0)

    verdict = await deps.policy.evaluate(
        case_id=state["case_id"],
        merchant_id=state["merchant_id"],
        facts=_facts_for(state, action, now),
    )
    effect = str(verdict.get("effect", PolicyEffect.DENY.value))

    await deps.audit.record(
        event_type=AuditEventType.POLICY_EVALUATED.value,
        actor="policy-engine",
        actor_kind="system",
        summary=f"{action['action_type']} -> {effect}"
        + (f" by {verdict.get('rule_name')}" if verdict.get("rule_name") else ""),
        detail=verdict,
        case_id=state["case_id"],
        action_id=action["action_id"],
        merchant_id=state["merchant_id"],
        thread_id=state["thread_id"],
        at=now,
    )

    updates: dict[str, Any] = {
        "policy_effect": effect,
        "policy_bundle_id": verdict.get("bundle_id"),
        "policy_rule_id": verdict.get("rule_id"),
    }
    capped = verdict.get("capped_amount_minor")
    if capped is not None and int(capped) < int(action.get("amount_minor", 0)):
        updates["capped_amount_minor"] = int(capped)
        updates["amount_minor"] = int(capped)
    if effect == PolicyEffect.DENY.value:
        updates["status"] = "denied_by_policy"

    return {
        "actions": replace_action(state, index, **updates),
        "history": note(
            state,
            "policy",
            f"{action['action_type']} {effect}: {verdict.get('reason', '')}"[:200],
            at=now.isoformat(),
        ),
    }


async def approval(deps: Deps, state: RecoveryState) -> dict[str, Any]:
    """Pause the case until a named human decides.

    The operator sees the model's own rationale verbatim, because approving an
    action whose reasoning you cannot read is not meaningfully being in the
    loop. They may approve, reject with feedback, or edit the payload -- and an
    edit is applied to the action before it executes, so the human's amendment
    is what actually happens rather than a suggestion the agent may ignore.
    """
    now = deps.clock.now()
    action = current_action(state)
    if action is None:
        return {"status": "planning"}
    index = state.get("current_action_index", 0)

    approval_id = await deps.approvals.request(
        case_id=state["case_id"],
        action=dict(action),
        thread_id=state["thread_id"],
        merchant_id=state["merchant_id"],
        escalation_reason=str(action.get("policy_effect", "policy required approval")),
        now=now,
    )
    await deps.audit.record(
        event_type=AuditEventType.APPROVAL_REQUESTED.value,
        actor="agent",
        actor_kind="agent",
        summary=f"{action['action_type']} queued for human approval",
        detail={"approval_id": approval_id, "rationale": action.get("rationale")},
        case_id=state["case_id"],
        action_id=action["action_id"],
        merchant_id=state["merchant_id"],
        thread_id=state["thread_id"],
        at=now,
    )

    decision: dict[str, Any] = interrupt(
        {
            "kind": "human_approval",
            "approval_id": approval_id,
            "case_id": state["case_id"],
            "action_id": action["action_id"],
            "action_type": action["action_type"],
            "amount_minor": action.get("amount_minor"),
            "rationale": action.get("rationale"),
            "confidence": action.get("model_confidence"),
        }
    )

    resumed_at = deps.clock.now()
    outcome = str(decision.get("decision", "reject"))
    await deps.audit.record(
        event_type=AuditEventType.APPROVAL_RESOLVED.value,
        actor=str(decision.get("decided_by", "operator")),
        actor_kind="operator",
        summary=f"{action['action_type']} {outcome} by {decision.get('decided_by', 'an operator')}",
        detail={"approval_id": approval_id, "note": decision.get("note")},
        case_id=state["case_id"],
        action_id=action["action_id"],
        merchant_id=state["merchant_id"],
        thread_id=state["thread_id"],
        at=resumed_at,
    )

    updates: dict[str, Any] = {"approval_id": approval_id}
    if outcome == "reject":
        updates["status"] = "rejected"
    else:
        updates["status"] = "approved"
        edited = decision.get("edited_payload")
        if outcome == "edit" and isinstance(edited, dict):
            # The human's amendment is what executes. Applying it here, before
            # the executor reads the action, is the difference between an
            # operator editing the action and an operator suggesting an edit.
            updates["payload"] = {**action.get("payload", {}), **edited}
            if "amount_minor" in edited:
                updates["amount_minor"] = int(edited["amount_minor"])

    return {
        "actions": replace_action(state, index, **updates),
        "human_decision": decision,
        "pending_approval_id": None,
        "status": "executing" if outcome != "reject" else "planning",
        "history": note(
            state,
            "approval",
            f"{action['action_type']} {outcome} by {decision.get('decided_by', 'an operator')}",
            at=resumed_at.isoformat(),
        ),
    }


def _facts_for(state: RecoveryState, action: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    """Assemble the fact set the policy engine evaluates.

    Only facts Anvil observed itself. The policy engine's own model validates
    this and rejects any key outside its catalogue, so a typo here is an error
    at the boundary rather than a rule that silently never matches.
    """
    from anvil.core.clock import ist_day_of_month, ist_hour

    action_type = ActionType(action["action_type"])
    return {
        "action_type": action["action_type"],
        "amount_minor": int(action.get("amount_minor", 0)),
        "currency": state["currency"],
        "failure_class": state.get("failure_class"),
        "hours_since_failure": state.get("hours_since_last_contact", 0),
        "case_attempt_count": state["attempts_made"],
        "mandate_cycle_attempt_count": state["attempts_made"],
        "case_contact_count": state["contacts_made"],
        "contacts_last_24h": state["contacts_last_24h"],
        "contacts_last_7d": state["contacts_last_7d"],
        "hours_since_last_contact": state["hours_since_last_contact"],
        "local_hour_ist": ist_hour(now),
        "local_day_of_month_ist": ist_day_of_month(now),
        "customer_tenure_days": state["customer_tenure_days"],
        "lifetime_value_minor": state["customer_lifetime_value_minor"],
        "prior_concession_count": state["prior_concession_count"],
        "prior_concessions_minor": state["prior_concessions_minor"],
        "customer_concession_headroom_minor": state["customer_concession_headroom_minor"],
        "subscription_mrr_minor": state["subscription_mrr_minor"],
        "budget_headroom_minor": state["budget_headroom_minor"],
        "purpose": action.get("payload", {}).get("purpose"),
        "consent_state": state["consent_state"],
        "authorisation_decision": action.get(
            "authorisation_decision", AuthorisationDecision.DENIED.value
        ),
        "recovery_likelihood": state.get("recovery_likelihood", 0),
        "churn_risk": state.get("churn_risk", 0),
        "merchant_review_first": state.get("merchant_review_first", True),
        "is_terminal_action": action_type.is_terminal,
    }
