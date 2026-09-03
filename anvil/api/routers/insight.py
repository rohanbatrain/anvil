"""The endpoints that let a reviewer interrogate the deterministic core.

Three things Anvil claims are hard to believe from prose: that retry timing is
genuinely optimised rather than guessed, that policy is genuinely deterministic
and fails closed, and that the ledger genuinely balances. Each gets an endpoint
here that can be poked with arbitrary inputs until the reviewer is satisfied or
finds a counter-example.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, HTTPException, Query

from anvil.api.schemas import (
    Amount,
    LedgerEntryView,
    LedgerTransactionView,
    PolicyBundleView,
    PolicyDecisionView,
    PolicyRuleView,
    RankedHour,
    RuleTraceView,
    ScheduleExplanation,
)
from anvil.core.clock import to_ist
from anvil.domain.enums import FailureClass
from anvil.domain.money import Money
from anvil.domain.taxonomy import RETRY_CURVES
from anvil.ledger.accounts import ChartOfAccounts
from anvil.ledger.posting import (
    PostingContext,
    grant_concession,
    recognise_receivable,
    settle_recovered_debit,
    write_off,
)
from anvil.policy.defaults import default_bundle
from anvil.policy.evaluator import evaluate
from anvil.policy.expressions import describe
from anvil.policy.facts import PolicyFacts

router = APIRouter(prefix="/api", tags=["insight"])

_BUNDLE = default_bundle()


def _ist(value: dt.datetime) -> str:
    return to_ist(value).strftime("%a %d %b %H:%M IST")


@router.get("/scheduler/explain", response_model=ScheduleExplanation)
async def explain_schedule(
    failure_class: FailureClass = Query(description="Which kind of failure to schedule for."),
    amount_minor: int = Query(default=1_499_00, gt=0, description="Amount at risk, in paise."),
    failed_at: dt.datetime | None = Query(default=None, description="When the debit failed."),
    attempts_used: int = Query(default=0, ge=0),
    mandate_attempts_remaining: int | None = Query(default=None, ge=0),
) -> ScheduleExplanation:
    """Solve the retry decision and show the candidates that lost.

    Deliberately parameterised on the failure instant, because the single most
    persuasive thing about the scheduler is that moving the failure a few days
    earlier or later changes its answer for a reason it can articulate.
    """
    from anvil.risk.scheduler import schedule_next_attempt

    when = failed_at or dt.datetime(2026, 9, 18, 6, 0, tzinfo=dt.UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)

    decision = schedule_next_attempt(
        failure_class=failure_class,
        amount_at_risk=Money(amount_minor),
        failed_at=when,
        now=when,
        attempts_used=attempts_used,
        mandate_attempts_remaining=mandate_attempts_remaining,
    )
    curve = RETRY_CURVES[failure_class]
    return ScheduleExplanation(
        should_retry=decision.should_retry,
        failure_class=failure_class.value,
        posture=decision.posture.value,
        attempt_number=decision.attempt_number,
        attempts_remaining=decision.attempts_remaining,
        at=decision.at.isoformat() if decision.at else None,
        ist_label=_ist(decision.at) if decision.at else None,
        probability_bps=decision.probability_bps,
        expected_value=Amount.of(decision.remaining_value or Money.zero()),
        explanation=decision.explanation,
        refusal_reason=decision.refusal_reason,
        rationale=curve.rationale,
        ranked=[
            RankedHour(
                at=hour.at.isoformat(),
                ist_label=_ist(hour.at),
                probability_bps=hour.probability_bps,
                value=Amount.of(Money(hour.value_minor)),
                is_chosen=hour.at == decision.at,
            )
            for hour in decision.ranked
        ],
    )


@router.get("/policy/bundle", response_model=PolicyBundleView)
async def policy_bundle() -> PolicyBundleView:
    return PolicyBundleView(
        id=_BUNDLE.id,
        version=_BUNDLE.version,
        content_hash=_BUNDLE.content_hash,
        rule_count=len(_BUNDLE.rules),
        immutable_count=len(_BUNDLE.immutable_rules),
        rules=[
            PolicyRuleView(
                id=rule.id,
                name=rule.name,
                priority=rule.priority,
                effect=rule.effect.value,
                description=rule.description,
                condition=describe(rule.conditions),
                cap_amount=(
                    Amount.of(Money(rule.cap_amount_minor))
                    if rule.cap_amount_minor is not None
                    else None
                ),
                cap_percent=rule.cap_percent,
                is_immutable=rule.is_immutable,
            )
            for rule in _BUNDLE.ordered
        ],
    )


@router.post("/policy/evaluate", response_model=PolicyDecisionView)
async def policy_evaluate(facts: PolicyFacts) -> PolicyDecisionView:
    """Evaluate arbitrary facts against the live bundle.

    The fact model forbids unknown keys, so a typo is a 422 at the boundary
    rather than a rule that silently never matches.
    """
    decision = evaluate(_BUNDLE, facts)
    return PolicyDecisionView(
        effect=decision.effect.value,
        allowed=decision.allowed,
        requires_approval=decision.requires_approval,
        denied=decision.denied,
        matched_rule_name=decision.matched_rule_name,
        reason=decision.reason,
        proposed=Amount.of(Money(facts.amount_minor, facts.currency)),
        effective=Amount.of(decision.effective_amount),
        was_capped=decision.was_capped,
        capping_rule_name=decision.capping_rule_name,
        trace=[
            RuleTraceView(
                rule_name=item.rule_name,
                priority=item.priority,
                effect=item.effect.value,
                matched=item.matched,
                condition=item.condition_summary,
                stopped_evaluation=item.stopped_evaluation,
            )
            for item in decision.trace
        ],
    )


@router.get("/ledger/demo", response_model=list[LedgerTransactionView])
async def ledger_demo(
    at_risk_minor: int = Query(default=1_499_00, gt=0),
    concession_minor: int = Query(default=200_00, ge=0),
    recover: bool = Query(default=True, description="Whether the debit eventually settles."),
) -> list[LedgerTransactionView]:
    """Build a real posting sequence and show that every transaction balances.

    Nothing is written; these are validated drafts. The balance check that runs
    here is the same one that runs before any commit, so a reviewer who can make
    this endpoint return ``balances: false`` has found a genuine defect.
    """
    if concession_minor >= at_risk_minor:
        raise HTTPException(
            status_code=422,
            detail="a concession cannot be larger than the amount at risk",
        )
    chart = ChartOfAccounts.derive("mch_demo", customer_ids=("cus_demo",))
    ctx = PostingContext(
        chart=chart,
        effective_at=dt.datetime(2026, 9, 30, 11, 0, tzinfo=dt.UTC),
        case_id="cse_demo",
        customer_id="cus_demo",
    )
    drafts = [recognise_receivable(ctx, Money(at_risk_minor))]
    remaining = at_risk_minor
    if concession_minor:
        drafts.append(grant_concession(ctx, Money(concession_minor)))
        remaining -= concession_minor
    if recover:
        drafts.append(settle_recovered_debit(ctx, Money(remaining)))
    else:
        drafts.append(write_off(ctx, Money(remaining), "no recovery within the horizon"))

    return [
        LedgerTransactionView(
            txn_type=draft.txn_type.value,
            narration=draft.narration,
            idempotency_key=draft.idempotency_key,
            balances=draft.imbalance_minor == 0,
            total_debits=Amount.of(draft.total_debits),
            total_credits=Amount.of(draft.total_credits),
            entries=[
                LedgerEntryView(
                    account=entry.account.label,
                    direction=entry.direction.value,
                    amount=Amount.of(entry.amount),
                )
                for entry in draft.entries
            ],
        )
        for draft in drafts
    ]
