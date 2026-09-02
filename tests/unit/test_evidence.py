"""Tests for arm assignment, the statistics, and the batch report.

The statistics tests matter more than they look. Every one of them guards
against a specific way an experiment write-up can mislead: comparing intervals
instead of differences, claiming significance from an underpowered sample,
reporting a lift with nothing to lift against, or losing money in the
arithmetic between gross and net.
"""

from __future__ import annotations

import pytest
from anvil.core.errors import ValidationError
from anvil.domain.enums import ExperimentArm, FailureClass
from anvil.domain.money import Money
from anvil.evidence.assignment import (
    DEFAULT_SPLIT,
    EVEN_SPLIT,
    ArmSplit,
    assign,
    realised_split,
)
from anvil.evidence.metrics import (
    EmptyControlArm,
    aggregate,
    as_json,
    compare,
    conserves_money,
    summarise,
)
from anvil.evidence.report import render
from anvil.evidence.statistics import (
    bootstrap_difference,
    bootstrap_proportion,
    is_significant,
    minimum_detectable_effect_bps,
    two_proportion_z,
)
from hypothesis import given, settings
from hypothesis import strategies as st

SEED = 20260902


class FakeOutcome:
    """The minimum an outcome needs to be aggregated."""

    def __init__(
        self,
        arm: ExperimentArm,
        *,
        at_risk: int = 1_499_00,
        recovered: int = 0,
        concession: int = 0,
        channel: int = 0,
        model: int = 0,
        attempts: int = 0,
        contacts: int = 0,
        failure_class: FailureClass | None = FailureClass.INSUFFICIENT_FUNDS,
        deterministic: bool | None = True,
        unmapped: bool = False,
        safety: int = 0,
        predictions: list[tuple[int, bool]] | None = None,
    ) -> None:
        self.arm = arm
        self.at_risk_minor = at_risk
        self.recovered_minor = recovered
        self.concession_minor = concession
        self.channel_cost_minor = channel
        self.model_cost_minor = model
        self.attempts = attempts
        self.contacts = contacts
        self.true_failure_class = failure_class
        self.classified_deterministically = deterministic
        self.code_was_unmapped = unmapped
        self.model_safety_events = safety
        self.predictions = predictions or []

    @property
    def recovered(self) -> bool:
        return self.recovered_minor > 0


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


def test_assignment_is_deterministic() -> None:
    """Anyone auditing the result must be able to recompute the arm."""
    first = assign(SEED, "cse_abc")
    second = assign(SEED, "cse_abc")
    assert first.arm is second.arm
    assert first.assignment_hash == second.assignment_hash


def test_a_different_seed_can_move_a_case() -> None:
    arms = {assign(s, "cse_abc", EVEN_SPLIT).arm for s in range(1, 61)}
    assert len(arms) > 1, "assignment must actually depend on the seed"


def test_the_realised_split_converges_on_the_requested_one() -> None:
    """A hash-based split must actually land where it says it will."""
    assignments = [assign(SEED, f"cse_{i}", EVEN_SPLIT) for i in range(6000)]
    realised = realised_split(assignments, EVEN_SPLIT)
    assert not realised.has_empty_arm
    for arm in ExperimentArm:
        assert abs(realised.drift_bps(arm)) < 250, (arm, realised.describe())


def test_a_split_that_does_not_sum_to_one_is_refused() -> None:
    """An unassigned remainder would silently drop cases out of the experiment."""
    with pytest.raises(ValidationError, match="sum to 10000"):
        ArmSplit(control_bps=5000, baseline_bps=5000, anvil_bps=5000)


def test_the_production_split_holds_back_a_real_control() -> None:
    """A control arm of zero is not a control arm."""
    assert DEFAULT_SPLIT.control_bps > 0
    assert DEFAULT_SPLIT.baseline_bps > 0


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def test_a_bootstrap_interval_contains_its_point_estimate() -> None:
    interval = bootstrap_proportion(300, 1000, seed=1)
    assert interval.low_bps <= interval.point_bps <= interval.high_bps
    assert interval.point_bps == 3000


def test_a_larger_sample_gives_a_tighter_interval() -> None:
    narrow = bootstrap_proportion(3000, 10_000, seed=1)
    wide = bootstrap_proportion(30, 100, seed=1)
    assert narrow.width_bps < wide.width_bps


def test_intervals_are_reproducible() -> None:
    assert bootstrap_proportion(300, 1000, seed=7) == bootstrap_proportion(300, 1000, seed=7)


def test_the_difference_interval_is_computed_on_the_difference() -> None:
    """Two overlapping arm intervals can still have a non-zero difference.

    This is the exact mistake the module exists to prevent: eyeballing overlap
    is a different, weaker and wrongly-calibrated test.
    """
    a = bootstrap_proportion(520, 1000, seed=1)
    b = bootstrap_proportion(480, 1000, seed=2)
    overlap = a.low_bps <= b.high_bps and b.low_bps <= a.high_bps
    assert overlap, "these arms were chosen to overlap"

    difference = bootstrap_difference(520, 1000, 480, 1000, seed=1)
    # The point estimate is the real difference, not something derived from the
    # overlap; whether it is significant is then decided by its own interval.
    assert difference.point_bps == 400


def test_a_clear_effect_is_significant() -> None:
    difference = bootstrap_difference(600, 1000, 300, 1000, seed=1)
    assert is_significant(difference)
    assert difference.low_bps > 0


def test_a_null_effect_is_not_significant() -> None:
    difference = bootstrap_difference(301, 1000, 300, 1000, seed=1)
    assert not is_significant(difference)
    assert difference.crosses_zero


def test_a_negative_effect_is_reported_as_negative() -> None:
    """The agent losing must be reportable, not just representable."""
    difference = bootstrap_difference(300, 1000, 600, 1000, seed=1)
    assert difference.point_bps < 0
    assert is_significant(difference)


def test_the_z_test_agrees_with_the_bootstrap_on_direction() -> None:
    for t, c in [(600, 300), (300, 600), (301, 300)]:
        difference = bootstrap_difference(t, 1000, c, 1000, seed=1)
        z = two_proportion_z(t, 1000, c, 1000)
        assert (difference.point_bps > 0) == (z > 0) or difference.point_bps == 0


def test_minimum_detectable_effect_shrinks_with_sample_size() -> None:
    small = minimum_detectable_effect_bps(2000, 50)
    large = minimum_detectable_effect_bps(2000, 5000)
    assert small > large


def test_an_empty_arm_yields_an_empty_interval_rather_than_a_crash() -> None:
    interval = bootstrap_proportion(0, 0, seed=1)
    assert interval.point_bps == 0
    assert interval.width_bps == 0


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_net_is_gross_less_every_cost() -> None:
    outcomes = [
        FakeOutcome(
            ExperimentArm.ANVIL,
            recovered=1_000_00,
            concession=200_00,
            channel=25,
            model=3,
        )
    ]
    result = aggregate(outcomes, ExperimentArm.ANVIL, seed=SEED)
    assert result.recovered == Money(1_000_00)
    assert result.total_cost == Money(200_00 + 25 + 3)
    assert result.net_recovered == Money(1_000_00 - 200_00 - 25 - 3)
    assert conserves_money(result)


@given(
    recovered=st.integers(0, 10_000_00),
    concession=st.integers(0, 500_00),
    channel=st.integers(0, 1_000),
    model=st.integers(0, 1_000),
)
@settings(max_examples=200, deadline=None)
def test_money_arithmetic_conserves(
    recovered: int, concession: int, channel: int, model: int
) -> None:
    outcomes = [
        FakeOutcome(
            ExperimentArm.ANVIL,
            recovered=recovered,
            concession=concession,
            channel=channel,
            model=model,
        )
    ]
    assert conserves_money(aggregate(outcomes, ExperimentArm.ANVIL, seed=SEED))


def test_lift_against_an_empty_control_is_refused() -> None:
    """A lift with no comparator is not a measurement."""
    treatment = aggregate(
        [FakeOutcome(ExperimentArm.ANVIL, recovered=100)], ExperimentArm.ANVIL, seed=SEED
    )
    empty = aggregate([], ExperimentArm.CONTROL, seed=SEED)
    with pytest.raises(EmptyControlArm):
        compare(treatment, empty, seed=SEED)


def test_recovery_is_broken_out_by_failure_class() -> None:
    """A headline number must not be able to hide one class doing all the work."""
    outcomes = [
        FakeOutcome(
            ExperimentArm.ANVIL, recovered=100, failure_class=FailureClass.ISSUER_TECHNICAL
        ),
        FakeOutcome(ExperimentArm.ANVIL, recovered=0, failure_class=FailureClass.MANDATE_REVOKED),
        FakeOutcome(ExperimentArm.ANVIL, recovered=0, failure_class=FailureClass.MANDATE_REVOKED),
    ]
    result = aggregate(outcomes, ExperimentArm.ANVIL, seed=SEED)
    assert result.by_failure_class["issuer_technical"] == (1, 1)
    assert result.by_failure_class["mandate_revoked"] == (2, 0)


def test_value_recovered_is_a_share_of_what_was_at_risk() -> None:
    outcomes = [FakeOutcome(ExperimentArm.ANVIL, at_risk=1_000_00, recovered=250_00)]
    result = aggregate(outcomes, ExperimentArm.ANVIL, seed=SEED)
    assert result.value_recovered_bps == 2500


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _mixed_batch() -> list[FakeOutcome]:
    out: list[FakeOutcome] = []
    for i in range(120):
        out.append(FakeOutcome(ExperimentArm.CONTROL, recovered=1_499_00 if i < 24 else 0))
    for i in range(120):
        out.append(
            FakeOutcome(
                ExperimentArm.BASELINE,
                recovered=1_499_00 if i < 90 else 0,
                channel=25,
                attempts=2,
                predictions=[(5500, i < 90)],
            )
        )
    for i in range(120):
        out.append(
            FakeOutcome(
                ExperimentArm.ANVIL,
                recovered=1_499_00 if i < 78 else 0,
                attempts=1,
                deterministic=i % 5 != 0,
                unmapped=i % 5 == 0,
                predictions=[(6200, i < 78)],
            )
        )
    return out


def test_the_report_states_significance_in_words() -> None:
    summary = summarise(_mixed_batch(), seed=SEED, population_size=2000)
    text = render(summary, model_available=False)
    assert "STATISTICALLY SIGNIFICANT" in text


def test_the_report_never_shows_a_lift_without_an_interval() -> None:
    summary = summarise(_mixed_batch(), seed=SEED, population_size=2000)
    text = render(summary, model_available=False)
    for line in text.splitlines():
        if "recovery rate difference" in line:
            assert "[" in line and "]" in line, line


def test_the_report_says_when_the_baseline_wins() -> None:
    """The finding this batch actually produced. It must not be possible to hide."""
    outcomes = [
        *[FakeOutcome(ExperimentArm.CONTROL, recovered=0) for _ in range(100)],
        *[FakeOutcome(ExperimentArm.BASELINE, recovered=1_499_00) for _ in range(100)],
        *[
            FakeOutcome(ExperimentArm.ANVIL, recovered=1_499_00 if i < 60 else 0)
            for i in range(100)
        ],
    ]
    summary = summarise(outcomes, seed=SEED, population_size=1000)
    text = render(summary, model_available=False)
    assert "outperformed the agent on raw recovery rate" in text


def test_the_report_always_carries_a_limitations_section() -> None:
    summary = summarise(_mixed_batch(), seed=SEED, population_size=2000)
    text = render(summary, model_available=False)
    assert "WHAT THIS RUN DOES NOT SHOW" in text
    assert "Approvals were auto-resolved" in text


def test_the_report_discloses_that_the_model_was_unavailable() -> None:
    summary = summarise(_mixed_batch(), seed=SEED, population_size=2000)
    assert "UNAVAILABLE" in render(summary, model_available=False)
    assert "classification available" in render(summary, model_available=True)


def test_the_report_is_stable_for_a_fixed_input() -> None:
    summary = summarise(_mixed_batch(), seed=SEED, population_size=2000)
    assert render(summary, model_available=False) == render(summary, model_available=False)


def test_the_json_payload_carries_every_arm_and_comparison() -> None:
    summary = summarise(_mixed_batch(), seed=SEED, population_size=2000)
    payload = as_json(summary)
    assert set(payload["arms"]) == {"control", "baseline", "anvil"}  # type: ignore[arg-type]
    assert payload["comparisons"]
    for comparison in payload["comparisons"]:  # type: ignore[union-attr]
        assert "ci_bps" in comparison
        assert "significant" in comparison
