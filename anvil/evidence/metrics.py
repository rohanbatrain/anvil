"""Aggregating case outcomes into per-arm results.

Money arithmetic is the whole job, and it has one rule: net is what is left
after everything the recovery cost. A report that shows gross recovery next to
a rival's gross recovery, without the concessions and the channel and model
spend that bought it, is not a comparison.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from anvil.domain.enums import ExperimentArm, FailureClass
from anvil.domain.money import Money
from anvil.evidence.statistics import (
    Interval,
    bootstrap_difference,
    bootstrap_proportion,
    is_significant,
    minimum_detectable_effect_bps,
    two_proportion_z,
)


class Outcome(Protocol):
    """The shape :mod:`anvil.simulator.world` produces. Declared, not imported."""

    arm: ExperimentArm
    at_risk_minor: int
    recovered_minor: int
    concession_minor: int
    channel_cost_minor: int
    model_cost_minor: int
    attempts: int
    contacts: int
    true_failure_class: FailureClass | None
    classified_deterministically: bool | None
    code_was_unmapped: bool
    model_safety_events: int
    predictions: list[tuple[int, bool]]

    @property
    def recovered(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class ArmResult:
    arm: ExperimentArm
    case_count: int
    recovered_count: int
    at_risk: Money
    recovered: Money
    concessions: Money
    channel_cost: Money
    model_cost: Money
    attempts: int
    contacts: int
    rate: Interval
    by_failure_class: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def net_recovered(self) -> Money:
        return self.recovered - self.concessions - self.channel_cost - self.model_cost

    @property
    def total_cost(self) -> Money:
        return self.concessions + self.channel_cost + self.model_cost

    @property
    def value_recovered_bps(self) -> int:
        """Share of the money at risk that came back, in basis points."""
        if self.at_risk.is_zero:
            return 0
        return int(self.recovered.minor * 10_000 / self.at_risk.minor)

    @property
    def cost_per_recovered_rupee_bps(self) -> int:
        if self.recovered.is_zero:
            return 0
        return int(self.total_cost.minor * 10_000 / self.recovered.minor)

    @property
    def attempts_per_recovery(self) -> float:
        return self.attempts / self.recovered_count if self.recovered_count else 0.0


@dataclass(frozen=True, slots=True)
class Comparison:
    """One arm measured against another, honestly."""

    treatment: ExperimentArm
    against: ExperimentArm
    difference: Interval
    z_score: float
    significant: bool
    minimum_detectable_bps: int
    net_difference: Money

    @property
    def underpowered(self) -> bool:
        """True when the batch could not have detected an effect this small."""
        return not self.significant and abs(self.difference.point_bps) < self.minimum_detectable_bps


class EmptyControlArm(Exception):
    """Raised rather than reporting a lift with nothing to lift against."""


def aggregate(outcomes: Sequence[Outcome], arm: ExperimentArm, *, seed: int) -> ArmResult:
    """Roll one arm's cases into a result."""
    selected = [o for o in outcomes if o.arm is arm]
    n = len(selected)
    recovered_count = sum(1 for o in selected if o.recovered)

    by_class: dict[str, tuple[int, int]] = {}
    for o in selected:
        key = o.true_failure_class.value if o.true_failure_class else "unclassified"
        total, won = by_class.get(key, (0, 0))
        by_class[key] = (total + 1, won + (1 if o.recovered else 0))

    return ArmResult(
        arm=arm,
        case_count=n,
        recovered_count=recovered_count,
        at_risk=Money(sum(o.at_risk_minor for o in selected)),
        recovered=Money(sum(o.recovered_minor for o in selected)),
        concessions=Money(sum(o.concession_minor for o in selected)),
        channel_cost=Money(sum(o.channel_cost_minor for o in selected)),
        model_cost=Money(sum(o.model_cost_minor for o in selected)),
        attempts=sum(o.attempts for o in selected),
        contacts=sum(o.contacts for o in selected),
        rate=bootstrap_proportion(recovered_count, n, seed=seed + arm.value.__hash__() % 1000),
        by_failure_class=by_class,
    )


def compare(treatment: ArmResult, against: ArmResult, *, seed: int) -> Comparison:
    """Compare two arms. Refuses to invent a lift against an empty arm."""
    if against.case_count == 0:
        raise EmptyControlArm(
            f"cannot report lift against {against.arm.value}: it has no cases. "
            "A lift with no comparator is not a measurement."
        )
    difference = bootstrap_difference(
        treatment.recovered_count,
        treatment.case_count,
        against.recovered_count,
        against.case_count,
        seed=seed,
    )
    return Comparison(
        treatment=treatment.arm,
        against=against.arm,
        difference=difference,
        z_score=two_proportion_z(
            treatment.recovered_count,
            treatment.case_count,
            against.recovered_count,
            against.case_count,
        ),
        significant=is_significant(difference),
        minimum_detectable_bps=minimum_detectable_effect_bps(
            against.rate.point_bps, min(treatment.case_count, against.case_count)
        ),
        net_difference=treatment.net_recovered - against.net_recovered,
    )


@dataclass(frozen=True, slots=True)
class BatchSummary:
    """Everything the report renders, computed once."""

    seed: int
    population_size: int
    case_count: int
    arms: dict[ExperimentArm, ArmResult]
    comparisons: list[Comparison]
    #: Cases whose reason code no table recognised, and how they were handled.
    unmapped_codes: int
    classified_deterministically: int
    classified_by_model: int
    model_safety_events: int
    predictions: list[tuple[int, bool]]

    @property
    def total_at_risk(self) -> Money:
        total = Money.zero()
        for r in self.arms.values():
            total = total + r.at_risk
        return total


def summarise(outcomes: Sequence[Outcome], *, seed: int, population_size: int) -> BatchSummary:
    arms = {
        arm: aggregate(outcomes, arm, seed=seed)
        for arm in ExperimentArm
        if any(o.arm is arm for o in outcomes)
    }
    comparisons: list[Comparison] = []
    control = arms.get(ExperimentArm.CONTROL)
    if control is not None and control.case_count:
        for arm, result in arms.items():
            if arm is ExperimentArm.CONTROL:
                continue
            comparisons.append(compare(result, control, seed=seed))
        baseline = arms.get(ExperimentArm.BASELINE)
        anvil = arms.get(ExperimentArm.ANVIL)
        if baseline is not None and anvil is not None and baseline.case_count:
            comparisons.append(compare(anvil, baseline, seed=seed + 1))

    anvil_cases = [o for o in outcomes if o.arm is ExperimentArm.ANVIL]
    return BatchSummary(
        seed=seed,
        population_size=population_size,
        case_count=len(outcomes),
        arms=arms,
        comparisons=comparisons,
        unmapped_codes=sum(1 for o in outcomes if o.code_was_unmapped),
        classified_deterministically=sum(
            1 for o in anvil_cases if o.classified_deterministically is True
        ),
        classified_by_model=sum(1 for o in anvil_cases if o.classified_deterministically is False),
        model_safety_events=sum(o.model_safety_events for o in outcomes),
        predictions=[p for o in outcomes for p in o.predictions],
    )


def conserves_money(result: ArmResult) -> bool:
    """Net must equal gross less every cost. Checked, not assumed."""
    expected = (
        result.recovered.minor
        - result.concessions.minor
        - result.channel_cost.minor
        - result.model_cost.minor
    )
    return result.net_recovered.minor == expected


def as_json(summary: BatchSummary) -> dict[str, Any]:
    """The structure the API serves and the console renders."""
    return {
        "seed": summary.seed,
        "population_size": summary.population_size,
        "case_count": summary.case_count,
        "total_at_risk_minor": summary.total_at_risk.minor,
        "unmapped_codes": summary.unmapped_codes,
        "classified_deterministically": summary.classified_deterministically,
        "classified_by_model": summary.classified_by_model,
        "model_safety_events": summary.model_safety_events,
        "arms": {
            arm.value: {
                "cases": r.case_count,
                "recovered": r.recovered_count,
                "rate_bps": r.rate.point_bps,
                "rate_ci_bps": [r.rate.low_bps, r.rate.high_bps],
                "at_risk_minor": r.at_risk.minor,
                "recovered_minor": r.recovered.minor,
                "net_recovered_minor": r.net_recovered.minor,
                "concessions_minor": r.concessions.minor,
                "channel_cost_minor": r.channel_cost.minor,
                "model_cost_minor": r.model_cost.minor,
                "attempts": r.attempts,
                "contacts": r.contacts,
                "by_failure_class": r.by_failure_class,
            }
            for arm, r in summary.arms.items()
        },
        "comparisons": [
            {
                "treatment": c.treatment.value,
                "against": c.against.value,
                "difference_bps": c.difference.point_bps,
                "ci_bps": [c.difference.low_bps, c.difference.high_bps],
                "significant": c.significant,
                "underpowered": c.underpowered,
                "minimum_detectable_bps": c.minimum_detectable_bps,
                "z": round(c.z_score, 3),
            }
            for c in summary.comparisons
        ],
    }
