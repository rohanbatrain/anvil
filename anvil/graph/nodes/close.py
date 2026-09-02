"""Closing a case, and saying honestly how it ended.

Four terminal outcomes, and the distinction between them is not cosmetic. A
merchant's dashboard that reports "closed" for both a recovered invoice and an
abandoned one is useless, and the evidence harness needs the difference to
compute a recovery rate at all.

``ABANDONED`` in particular is a success of a kind: it means a stopping rule
fired and Anvil chose to stop spending attempts and goodwill on a case that was
not going to come back. A system that never abandons anything is not persistent,
it is just expensive.
"""

from __future__ import annotations

from typing import Any

from anvil.domain.enums import AuditEventType, CaseStatus, FailureClass, RetryPosture
from anvil.domain.taxonomy import RETRY_CURVES
from anvil.graph.deps import Deps
from anvil.graph.state import RecoveryState, note


def decide_closure(state: RecoveryState) -> tuple[CaseStatus, str]:
    """Work out which status this case earned, and why.

    Pure, so the classification of an outcome can be tested without running a
    graph, and so the same logic can label historical cases during a backfill.

    Note that this does not always return a *terminal* status. A case whose last
    gateway call timed out is parked in ``PENDING_RECONCILIATION``, which is
    deliberately not terminal: we do not know whether that debit took the
    customer's money, and calling it abandoned would be a claim we cannot
    support -- as well as writing off a receivable that may already be settled.
    """
    recovered = state.get("amount_recovered_minor", 0)
    at_risk = state.get("amount_at_risk_minor", 0)

    if state.get("status") == CaseStatus.PENDING_RECONCILIATION.value:
        return (
            CaseStatus.PENDING_RECONCILIATION,
            "The last debit attempt returned no answer, so it is unknown whether the "
            "customer was charged. The reconciler will resolve it against the gateway "
            "using the original idempotency key. Nothing is written off until it does.",
        )

    if recovered >= at_risk > 0:
        concession = state.get("concession_granted_minor", 0)
        if concession:
            return (
                CaseStatus.RECOVERED,
                f"Recovered {recovered / 100:,.2f} after conceding "
                f"{concession / 100:,.2f} against the authorised budget.",
            )
        return CaseStatus.RECOVERED, f"Recovered {recovered / 100:,.2f} in full."

    failure_class = FailureClass(state.get("failure_class", FailureClass.UNKNOWN.value))
    curve = RETRY_CURVES[failure_class]

    if curve.posture is RetryPosture.NEVER and recovered == 0:
        if failure_class is FailureClass.MANDATE_REVOKED:
            return (
                CaseStatus.CHURNED,
                "The customer revoked their mandate. That is a decision, not a payment "
                "failure, and the only honest label for it is churn.",
            )
        return (
            CaseStatus.UNRECOVERABLE,
            f"{failure_class.value} cannot be recovered by any action in Anvil's authority. "
            f"{curve.rationale}",
        )

    if recovered > 0:
        return (
            CaseStatus.RECOVERED,
            f"Partially recovered {recovered / 100:,.2f} of {at_risk / 100:,.2f}.",
        )

    return (
        CaseStatus.ABANDONED,
        f"Stopped after {state.get('attempts_made', 0)} attempt(s) and "
        f"{state.get('contacts_made', 0)} contact(s). Continuing would spend issuer "
        "goodwill and customer patience on a case the evidence says will not settle.",
    )


async def close(deps: Deps, state: RecoveryState) -> dict[str, Any]:
    """Write the terminal state, and write off anything genuinely lost.

    The write-off matters for the ledger's integrity: the receivable was
    recognised when the case opened, so leaving it on the books after
    abandonment would overstate what the merchant is owed indefinitely.
    """
    now = deps.clock.now()
    status, reason = decide_closure(state)

    outstanding = state["amount_at_risk_minor"] - state.get("amount_recovered_minor", 0)
    # Never write off a receivable whose fate is unknown. An unresolved attempt
    # may already have taken the money; writing it off would understate what the
    # merchant is owed and would have to be reversed the moment it resolves.
    if (
        status is not CaseStatus.RECOVERED
        and status is not CaseStatus.PENDING_RECONCILIATION
        and outstanding > 0
    ):
        await deps.ledger.write_off(
            case_id=state["case_id"],
            customer_id=state["customer_id"],
            merchant_id=state["merchant_id"],
            amount_minor=outstanding,
            reason=reason,
            at=now,
        )

    await deps.audit.record(
        event_type=AuditEventType.CASE_CLOSED.value,
        actor="agent",
        actor_kind="agent",
        summary=f"case closed as {status.value}: {reason}"[:400],
        detail={
            "recovered_minor": state.get("amount_recovered_minor", 0),
            "conceded_minor": state.get("concession_granted_minor", 0),
            "written_off_minor": max(0, outstanding) if status is not CaseStatus.RECOVERED else 0,
            "attempts": state.get("attempts_made", 0),
            "contacts": state.get("contacts_made", 0),
            "model_safety_events": state.get("model_safety_events", 0),
            "degraded": state.get("degraded", False),
        },
        case_id=state["case_id"],
        action_id=None,
        merchant_id=state["merchant_id"],
        thread_id=state["thread_id"],
        at=now,
    )
    await deps.cases.sync({**state, "status": status.value}, now=now)

    return {
        "status": status.value,
        "closure_reason": reason,
        "closed_at": now.isoformat(),
        "history": note(state, "close", f"{status.value}: {reason}"[:200], at=now.isoformat()),
    }
