"""Cases and the approval queue -- the operational surface.

The approval endpoints are the ones worth reading. Each queue item is a real
LangGraph thread paused on a committed checkpoint, so approving from the browser
resumes a graph that then runs authorisation, policy and the executor for real.
The pause is not simulated for the demo; the demo is simulated *around* a real
pause.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from anvil.api.schemas import (
    ActionView,
    Amount,
    ApprovalDecisionRequest,
    ApprovalItem,
    ApprovalResult,
    CaseDetail,
    CaseSummary,
    TimelineItem,
)
from anvil.api.state import LiveCase, get_state, resolve_approval
from anvil.core.clock import to_ist
from anvil.core.errors import NotFound, OptimisticLockConflict
from anvil.domain.money import Money

router = APIRouter(prefix="/api", tags=["operations"])


def _amount(minor: int | None) -> Amount | None:
    return None if minor is None else Amount.of(Money(minor))


def _summary_from_live(live: LiveCase) -> CaseSummary:
    state = live.state
    case = live.case
    return CaseSummary(
        case_id=case.case_id,
        customer_name=case.customer.display_name,
        customer_id=case.customer.customer_id,
        arm=case.arm.value,
        status=str(state.get("status", "open")),
        at_risk=Amount.of(case.amount),
        recovered=Amount.of(Money(int(state.get("amount_recovered_minor", 0)))),
        failure_class=case.true_failure_class.value,
        observed_failure_class=state.get("failure_class"),
        raw_code=case.raw_code,
        code_is_unmapped=case.code_is_unmapped,
        classified_deterministically=state.get("classified_deterministically"),
        attempts=int(state.get("attempts_made", 0)),
        contacts=int(state.get("contacts_made", 0)),
        bank=case.customer.bank.name,
        mandate_type=case.customer.authorisation.auth_type.value,
        recovered_flag=int(state.get("amount_recovered_minor", 0)) > 0,
    )


@router.get("/cases", response_model=list[CaseSummary])
async def list_cases(
    limit: int = Query(default=60, ge=1, le=500),
    unmapped_only: bool = Query(
        default=False,
        description="Only cases whose reason code no table recognises — the ones the "
        "language model exists to handle.",
    ),
) -> list[CaseSummary]:
    """The at-risk book. Live cases first, since those have real state."""
    state = get_state()
    out: list[CaseSummary] = [_summary_from_live(live) for live in state.live.values()]
    seen = {item.case_id for item in out}

    for case in state.cases:
        if len(out) >= limit:
            break
        if case.case_id in seen:
            continue
        if unmapped_only and not case.code_is_unmapped:
            continue
        out.append(
            CaseSummary(
                case_id=case.case_id,
                customer_name=case.customer.display_name,
                customer_id=case.customer.customer_id,
                arm=case.arm.value,
                status="open",
                at_risk=Amount.of(case.amount),
                recovered=Amount.of(Money.zero()),
                failure_class=case.true_failure_class.value,
                observed_failure_class=None,
                raw_code=case.raw_code,
                code_is_unmapped=case.code_is_unmapped,
                classified_deterministically=None,
                attempts=0,
                contacts=0,
                bank=case.customer.bank.name,
                mandate_type=case.customer.authorisation.auth_type.value,
                recovered_flag=False,
            )
        )
    return out[:limit]


@router.get("/cases/{case_id}", response_model=CaseDetail)
async def case_detail(case_id: str) -> CaseDetail:
    """One case, with its full timeline and every action's legitimacy trail."""
    state = get_state()
    live = next((item for item in state.live.values() if item.case_id == case_id), None)
    try:
        case = live.case if live else state.case_by_id(case_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc

    graph_state = live.state if live else {}
    auth = case.customer.authorisation

    ledger_views = []
    if live is not None:
        from anvil.api.routers.insight import ledger_demo

        ledger_views = await ledger_demo(
            at_risk_minor=case.amount.minor,
            concession_minor=int(graph_state.get("concession_granted_minor", 0)),
            recover=int(graph_state.get("amount_recovered_minor", 0)) > 0,
        )

    base = _summary_from_live(live) if live else (await list_cases(limit=500))[0]
    if not live:
        base = next((item for item in await list_cases(limit=500) if item.case_id == case_id), base)

    return CaseDetail(
        **base.model_dump(),
        narration=case.narration,
        failed_at=to_ist(case.failed_at).strftime("%a %d %b %Y, %H:%M IST"),
        mandate_reference=auth.external_reference,
        mandate_max=Amount.of(auth.max_amount),
        tenure_days=case.customer.tenure_days,
        language=case.customer.traits.language,
        timeline=[
            TimelineItem(
                node=str(item.get("node", "")),
                summary=str(item.get("summary", "")),
                at=str(item.get("at", "")),
            )
            for item in graph_state.get("history", [])
        ],
        actions=[
            ActionView(
                action_id=str(action.get("action_id", "")),
                action_type=str(action.get("action_type", "")),
                status=str(action.get("status", "proposed")),
                amount=_amount(action.get("amount_minor")),
                rationale=action.get("rationale"),
                authorisation_decision=action.get("authorisation_decision"),
                policy_effect=action.get("policy_effect"),
                scheduled_for=action.get("scheduled_for"),
            )
            for action in graph_state.get("actions", [])
        ],
        ledger=ledger_views,
        degraded=bool(graph_state.get("degraded", False)),
        degraded_reason=graph_state.get("degraded_reason"),
        model_safety_events=int(graph_state.get("model_safety_events", 0)),
    )


@router.get("/approvals", response_model=list[ApprovalItem])
async def list_approvals() -> list[ApprovalItem]:
    """Everything waiting on a person. Each one is a genuinely paused graph."""
    state = get_state()
    items: list[ApprovalItem] = []
    for live in state.live.values():
        if live.resolved:
            continue
        payload = live.interrupt
        items.append(
            ApprovalItem(
                approval_id=live.approval_id,
                case_id=live.case_id,
                customer_name=live.case.customer.display_name,
                action_type=str(payload.get("action_type", payload.get("kind", "action"))),
                amount=_amount(payload.get("amount_minor")),
                rationale=payload.get("rationale"),
                escalation_reason=str(
                    payload.get("reason")
                    or "policy required a human decision before this action executes"
                ),
                requested_at=to_ist(live.case.failed_at).strftime("%d %b %H:%M IST"),
                at_risk=Amount.of(live.case.amount),
                failure_class=live.state.get("failure_class"),
                recovery_likelihood=live.state.get("recovery_likelihood"),
                churn_risk=live.state.get("churn_risk"),
                version=live.version,
            )
        )
    return items


@router.post("/approvals/{approval_id}", response_model=ApprovalResult)
async def decide_approval(approval_id: str, request: ApprovalDecisionRequest) -> ApprovalResult:
    """Resume the paused graph with a human decision.

    A version mismatch returns 409 rather than overwriting: two operators who
    opened the same item must not both be able to resolve it.
    """
    state = get_state()
    try:
        live = await resolve_approval(
            state,
            approval_id,
            decision=request.decision,
            decided_by=request.decided_by,
            version=request.version,
            note=request.note,
            edited_amount_minor=request.edited_amount_minor,
        )
    except OptimisticLockConflict as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc

    return ApprovalResult(
        approval_id=approval_id,
        outcome=request.decision,
        case_status=str(live.state.get("status", "unknown")),
        recovered=Amount.of(Money(int(live.state.get("amount_recovered_minor", 0)))),
        timeline=[
            TimelineItem(
                node=str(item.get("node", "")),
                summary=str(item.get("summary", "")),
                at=str(item.get("at", "")),
            )
            for item in live.state.get("history", [])
        ],
    )
