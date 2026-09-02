"""Tests for classification, scheduling, scoring, calibration and detection.

The scheduler tests are the important ones. A retry optimiser that cannot be
shown to prefer payday for a balance failure and a fast retry for a technical
one is just an expensive way to pick a random hour.
"""

from __future__ import annotations

import datetime as dt

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from anvil.core.clock import IST
from anvil.domain.enums import FailureClass, RetryPosture
from anvil.domain.money import Money
from anvil.risk.calibration import Prediction, calibrate, render_reliability_table
from anvil.risk.classifier import classify_failure
from anvil.risk.detection import (
    AttemptRecord,
    Detection,
    RiskSignal,
    SubscriptionSnapshot,
    detect,
    detect_all,
    total_at_risk,
)
from anvil.risk.scheduler import (
    MIN_GAP_HOURS,
    schedule_next_attempt,
    value_of_retrying,
)
from anvil.risk.scoring import (
    CustomerHistory,
    churn_risk,
    priority,
    recovery_likelihood,
    score_case,
)

AMOUNT = Money(1_499_00)
# 18 Sep 2026 is mid-cycle, when balances are thinnest -- the hardest starting
# point for the scheduler, and therefore the most informative one.
MID_CYCLE_FAILURE = dt.datetime(2026, 9, 18, 6, 0, tzinfo=dt.UTC)

NEVER_CLASSES = [
    FailureClass.INSTRUMENT_EXPIRED,
    FailureClass.MANDATE_REVOKED,
    FailureClass.ACCOUNT_CLOSED,
    FailureClass.RISK_DECLINED,
]


def schedule(fc: FailureClass, **kwargs: object):
    params: dict[str, object] = {
        "failure_class": fc,
        "amount_at_risk": AMOUNT,
        "failed_at": MID_CYCLE_FAILURE,
        "now": MID_CYCLE_FAILURE,
    }
    params.update(kwargs)
    return schedule_next_attempt(**params)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The scheduler makes the decisions it claims to make
# ---------------------------------------------------------------------------


def test_insufficient_funds_waits_for_the_salary_credit() -> None:
    """The whole thesis of the module, in one assertion.

    Starting from a mid-cycle failure, the optimiser must be willing to wait
    nearly a fortnight to reach a payday, because that is where the money is.
    A greedy scheduler retries tomorrow and fails.
    """
    decision = schedule(FailureClass.INSUFFICIENT_FUNDS)
    assert decision.should_retry
    assert decision.at is not None
    local = decision.at.astimezone(IST)
    assert local.day >= 28 or local.day <= 3, f"chose the {local.day}th, not a payday"
    assert (decision.at - MID_CYCLE_FAILURE).days >= 7


def test_issuer_technical_retries_within_hours() -> None:
    """A rail failure is the cheapest recovery there is. Do not sit on it."""
    decision = schedule(FailureClass.ISSUER_TECHNICAL)
    assert decision.should_retry
    assert decision.at is not None
    assert (decision.at - MID_CYCLE_FAILURE) <= dt.timedelta(hours=24)
    assert decision.posture is RetryPosture.RETRY_FAST


def test_limit_exceeded_waits_for_the_reset() -> None:
    """Per-period caps reset on a boundary; retrying before it is wasted."""
    decision = schedule(FailureClass.LIMIT_EXCEEDED)
    assert decision.should_retry
    assert decision.at is not None
    assert (decision.at - MID_CYCLE_FAILURE) >= dt.timedelta(hours=24)


@pytest.mark.parametrize("failure_class", NEVER_CLASSES)
def test_terminal_classes_are_never_scheduled(failure_class: FailureClass) -> None:
    """Refusing is a decision, and it comes with a reason a human can read."""
    decision = schedule(failure_class)
    assert not decision.should_retry
    assert decision.refusal_reason
    assert decision.at is None
    assert decision.remaining_value == Money.zero()


def test_risk_declined_refusal_explains_the_harm() -> None:
    """Not merely futile -- actively damaging. The refusal has to say so."""
    decision = schedule(FailureClass.RISK_DECLINED)
    assert "harmful" in (decision.refusal_reason or "")


def test_never_schedules_before_the_minimum_gap() -> None:
    """Issuers treat rapid repeat presentments as abusive. The floor is absolute."""
    for fc in (FailureClass.ISSUER_TECHNICAL, FailureClass.INSUFFICIENT_FUNDS):
        decision = schedule(fc)
        assert decision.at is not None
        assert decision.at >= MID_CYCLE_FAILURE + dt.timedelta(hours=MIN_GAP_HOURS)


def test_never_schedules_past_mandate_expiry() -> None:
    """The authorisation is a hard boundary, not a preference."""
    expiry = MID_CYCLE_FAILURE + dt.timedelta(days=3)
    decision = schedule(FailureClass.INSUFFICIENT_FUNDS, mandate_valid_until=expiry)
    assert decision.should_retry
    assert decision.at is not None
    assert decision.at <= expiry


def test_refuses_when_the_mandate_expires_before_the_minimum_gap() -> None:
    expiry = MID_CYCLE_FAILURE + dt.timedelta(hours=1)
    decision = schedule(FailureClass.INSUFFICIENT_FUNDS, mandate_valid_until=expiry)
    assert not decision.should_retry
    assert "expires" in (decision.refusal_reason or "")


def test_respects_the_mandate_attempt_allowance() -> None:
    decision = schedule(FailureClass.INSUFFICIENT_FUNDS, mandate_attempts_remaining=1)
    assert decision.should_retry
    assert decision.attempts_remaining == 1

    exhausted = schedule(FailureClass.INSUFFICIENT_FUNDS, mandate_attempts_remaining=0)
    assert not exhausted.should_retry
    assert "no debit attempts left" in (exhausted.refusal_reason or "")


def test_refuses_once_the_curve_budget_is_spent() -> None:
    decision = schedule(FailureClass.INSUFFICIENT_FUNDS, attempts_used=4)
    assert not decision.should_retry
    assert "exhausted" in (decision.refusal_reason or "")


# ---------------------------------------------------------------------------
# The dynamic program behaves like a dynamic program
# ---------------------------------------------------------------------------


def test_more_remaining_attempts_is_never_worth_less() -> None:
    """Monotonicity in the attempt budget. A free extra option cannot hurt."""
    values = [
        value_of_retrying(
            failure_class=FailureClass.INSUFFICIENT_FUNDS,
            amount_at_risk=AMOUNT,
            failed_at=MID_CYCLE_FAILURE,
            now=MID_CYCLE_FAILURE,
            mandate_attempts_remaining=k,
        )
        for k in (1, 2, 3, 4)
    ]
    assert values == sorted(values), values


def test_remaining_value_never_exceeds_the_amount_at_risk() -> None:
    """You cannot expect to recover more than is owed."""
    for fc in FailureClass:
        value = value_of_retrying(
            failure_class=fc,
            amount_at_risk=AMOUNT,
            failed_at=MID_CYCLE_FAILURE,
            now=MID_CYCLE_FAILURE,
        )
        assert value <= AMOUNT, fc


def test_value_scales_linearly_with_the_amount_at_risk() -> None:
    """The optimiser's choice of hour must not depend on the ticket size."""
    small = schedule(FailureClass.INSUFFICIENT_FUNDS, amount_at_risk=Money(100_00))
    large = schedule(FailureClass.INSUFFICIENT_FUNDS, amount_at_risk=Money(10_000_00))
    assert small.at == large.at
    assert small.probability_bps == large.probability_bps


def test_the_chosen_hour_is_the_top_ranked_one() -> None:
    """The argmax and the ranking must agree, or the console shows a lie."""
    decision = schedule(FailureClass.INSUFFICIENT_FUNDS)
    assert decision.ranked
    assert decision.ranked[0].at == decision.at


def test_explanation_names_the_actual_driver() -> None:
    """The sentence cites the real factor, not a post-hoc story."""
    decision = schedule(FailureClass.INSUFFICIENT_FUNDS)
    assert "salary-credit" in decision.explanation
    technical = schedule(FailureClass.ISSUER_TECHNICAL)
    assert "maintenance window" in technical.explanation


@given(
    hours_offset=st.integers(min_value=0, max_value=24 * 40),
    attempts=st.integers(min_value=0, max_value=5),
)
@settings(max_examples=60, deadline=None)
def test_scheduler_is_total(hours_offset: int, attempts: int) -> None:
    """It always returns a decision and never raises, whatever it is handed."""
    failed = MID_CYCLE_FAILURE - dt.timedelta(hours=hours_offset)
    for fc in FailureClass:
        decision = schedule_next_attempt(
            failure_class=fc,
            amount_at_risk=AMOUNT,
            failed_at=failed,
            now=MID_CYCLE_FAILURE,
            attempts_used=attempts,
        )
        assert isinstance(decision.should_retry, bool)
        if decision.should_retry:
            assert decision.at is not None
            assert 0 <= decision.probability_bps <= 10_000
        else:
            assert decision.refusal_reason


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_recognised_codes_resolve_without_a_model() -> None:
    for raw, expected in [
        ("U30", FailureClass.ISSUER_TECHNICAL),
        ("54", FailureClass.INSTRUMENT_EXPIRED),
        ("insufficient_funds", FailureClass.INSUFFICIENT_FUNDS),
    ]:
        result = classify_failure(raw_code=raw)
        assert result.resolved, raw
        assert result.failure_class is expected  # type: ignore[union-attr]


def test_unrecognised_free_text_escalates_rather_than_guessing() -> None:
    """The case the LLM exists for. It must be handed over, not guessed."""
    result = classify_failure(raw_code="", gateway_description="something odd happened")
    assert not result.resolved
    assert result.should_escalate


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@given(
    tenure=st.integers(0, 3000),
    failures=st.integers(0, 30),
    recoveries=st.integers(0, 30),
    attempts=st.integers(0, 6),
    contacts=st.integers(0, 8),
)
@settings(max_examples=200, deadline=None)
def test_scores_stay_in_range(
    tenure: int, failures: int, recoveries: int, attempts: int, contacts: int
) -> None:
    history = CustomerHistory(
        tenure_days=tenure, prior_failures=failures, prior_recoveries=recoveries
    )
    for fc in FailureClass:
        scores = score_case(
            failure_class=fc,
            amount_at_risk=AMOUNT,
            history=history,
            attempts_used=attempts,
            contacts_made=contacts,
        )
        assert 0 <= scores.recovery_likelihood <= 1000
        assert 0 <= scores.churn_risk <= 1000
        assert 0 <= scores.priority <= 1000


def test_more_contacts_never_lowers_churn_risk() -> None:
    """The term that stops the agent sending a sixth reminder."""
    history = CustomerHistory(tenure_days=200)
    risks = [
        churn_risk(
            failure_class=FailureClass.INSUFFICIENT_FUNDS, history=history, contacts_made=c
        )
        for c in range(6)
    ]
    assert risks == sorted(risks)
    assert risks[-1] > risks[0]


def test_prior_recoveries_never_lower_recovery_likelihood() -> None:
    scores = [
        recovery_likelihood(
            failure_class=FailureClass.INSUFFICIENT_FUNDS,
            history=CustomerHistory(prior_failures=10 - r, prior_recoveries=r),
        )
        for r in range(0, 11)
    ]
    assert scores == sorted(scores)


def test_an_unknown_customer_is_not_treated_as_a_bad_payer() -> None:
    """A first failure is not evidence. The default sits at the midpoint."""
    assert CustomerHistory().recovery_rate_bps == 5000


def test_expired_card_is_recoverable_just_not_by_retrying() -> None:
    """The distinction the whole action space rests on."""
    expired = recovery_likelihood(
        failure_class=FailureClass.INSTRUMENT_EXPIRED, history=CustomerHistory()
    )
    closed = recovery_likelihood(
        failure_class=FailureClass.ACCOUNT_CLOSED, history=CustomerHistory()
    )
    assert expired > closed


def test_priority_rises_with_both_value_and_urgency() -> None:
    low = priority(amount_at_risk=Money(100_00), recovery=500, churn=100)
    more_money = priority(amount_at_risk=Money(10_000_00), recovery=500, churn=100)
    more_urgent = priority(amount_at_risk=Money(100_00), recovery=500, churn=900)
    assert more_money > low
    assert more_urgent > low


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def test_perfect_calibration_is_recognised() -> None:
    """1000 predictions at 30%, of which exactly 30% settle."""
    predictions = [Prediction(3000, i < 300) for i in range(1000)]
    report = calibrate(predictions)
    assert report.sufficient_sample
    assert report.observed_rate_bps == 3000
    assert report.expected_calibration_error_bps == 0
    assert "Well calibrated" in report.verdict


def test_overconfidence_is_named_as_overconfidence() -> None:
    predictions = [Prediction(9000, i < 400) for i in range(1000)]
    report = calibrate(predictions)
    assert report.is_overconfident
    assert "over-confident" in report.verdict


def test_brier_score_matches_a_hand_computation() -> None:
    """Two predictions: 100% that settled, 0% that did not. Perfect, so zero."""
    assert calibrate([Prediction(10_000, True), Prediction(0, False)]).brier_score_bps == 0
    # Both maximally wrong: squared error 1.0 each, mean 1.0 -> 10000 bps.
    assert calibrate([Prediction(10_000, False), Prediction(0, True)]).brier_score_bps == 10_000


def test_a_small_sample_refuses_to_draw_a_conclusion() -> None:
    """Presenting noise as evidence is the dishonesty this track penalises."""
    report = calibrate([Prediction(5000, True) for _ in range(12)])
    assert not report.sufficient_sample
    assert "too few" in report.verdict


def test_empty_calibration_says_so_rather_than_reporting_zero() -> None:
    report = calibrate([])
    assert report.count == 0
    assert "cannot be assessed" in report.verdict
    assert render_reliability_table(report).strip() == "(no completed attempts)"


def test_empty_buckets_are_omitted_not_reported_as_perfect() -> None:
    """An absence of evidence must not render as a zero gap."""
    report = calibrate([Prediction(3000, True), Prediction(3100, False)], buckets=10)
    assert len(report.buckets) == 1
    assert report.buckets[0].label == "30-40%"


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


NOW = dt.datetime(2026, 9, 20, 9, 0, tzinfo=dt.UTC)


def snapshot(**kwargs: object) -> SubscriptionSnapshot:
    base: dict[str, object] = {
        "subscription_id": "sub_1",
        "customer_id": "cus_1",
        "amount": AMOUNT,
        "current_period_end": NOW + dt.timedelta(days=10),
    }
    base.update(kwargs)
    return SubscriptionSnapshot(**base)  # type: ignore[arg-type]


def test_a_failed_debit_is_detected() -> None:
    found = detect(
        snapshot(
            consecutive_failures=1,
            recent_attempts=(
                AttemptRecord(
                    at=NOW - dt.timedelta(hours=2),
                    succeeded=False,
                    attempt_number=1,
                    failure_class=FailureClass.INSUFFICIENT_FUNDS,
                ),
            ),
        ),
        now=NOW,
    )
    assert any(d.signal is RiskSignal.DEBIT_FAILED for d in found)


def test_a_degrading_subscription_is_caught_before_it_fails() -> None:
    """Every cycle settled -- and it is still at risk. This is the early signal."""
    attempts = tuple(
        AttemptRecord(at=NOW - dt.timedelta(days=30 * i), succeeded=True, attempt_number=3)
        for i in range(1, 4)
    )
    found = detect(snapshot(recent_attempts=attempts), now=NOW)
    assert any(d.signal is RiskSignal.DEGRADING for d in found)
    assert all(d.signal is not RiskSignal.DEBIT_FAILED for d in found)


def test_expiring_mandate_and_instrument_are_both_flagged() -> None:
    found = detect(
        snapshot(
            mandate_valid_until=NOW + dt.timedelta(days=20),
            instrument_expires_at=NOW + dt.timedelta(days=30),
        ),
        now=NOW,
    )
    signals = {d.signal for d in found}
    assert RiskSignal.MANDATE_EXPIRING in signals
    assert RiskSignal.INSTRUMENT_EXPIRING in signals


def test_last_attempt_in_the_cycle_is_the_most_urgent_signal() -> None:
    found = detect(
        snapshot(
            consecutive_failures=2,
            mandate_attempts_remaining=1,
            recent_attempts=(
                AttemptRecord(at=NOW, succeeded=False, attempt_number=2),
            ),
        ),
        now=NOW,
    )
    assert found[0].signal is RiskSignal.ATTEMPTS_NEARLY_EXHAUSTED


def test_subscriptions_already_being_worked_are_skipped() -> None:
    """Two cases for one subscription would double-count and double-contact."""
    at_risk = snapshot(consecutive_failures=1, has_open_case=True)
    assert detect_all([at_risk], now=NOW) == []
    assert detect_all([at_risk], now=NOW, skip_with_open_case=False) != []


def test_money_at_risk_counts_each_subscription_once() -> None:
    """Three signals on one subscription is not three times the money."""
    found = detect(
        snapshot(
            consecutive_failures=1,
            mandate_valid_until=NOW + dt.timedelta(days=10),
            instrument_expires_at=NOW + dt.timedelta(days=12),
            recent_attempts=(AttemptRecord(at=NOW, succeeded=False, attempt_number=1),),
        ),
        now=NOW,
    )
    assert len(found) >= 3
    assert total_at_risk(found) == AMOUNT


def test_detections_are_ordered_most_urgent_first() -> None:
    found: list[Detection] = detect(
        snapshot(
            consecutive_failures=3,
            mandate_valid_until=NOW + dt.timedelta(days=5),
            recent_attempts=(AttemptRecord(at=NOW, succeeded=False, attempt_number=3),),
        ),
        now=NOW,
    )
    assert [d.urgency for d in found] == sorted((d.urgency for d in found), reverse=True)
