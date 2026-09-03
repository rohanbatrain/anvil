"""The batch experiment, served to the cockpit.

Results are cached per (seed, size, model-availability) because a batch is a
few seconds of work and a dashboard that re-runs the experiment on every page
load would invite someone to reload until they liked the number. The cache key
includes everything that changes the result, so a different seed is a different
experiment rather than a stale answer.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query

from anvil.api.schemas import (
    Amount,
    ArmView,
    BatchView,
    CalibrationBucketView,
    ComparisonView,
)
from anvil.domain.enums import ExperimentArm
from anvil.evidence.assignment import EVEN_SPLIT
from anvil.evidence.metrics import BatchSummary, summarise
from anvil.evidence.report import _ARM_LABELS, _limitations
from anvil.risk.calibration import Prediction, calibrate
from anvil.simulator.population import build_population
from anvil.simulator.world import World

router = APIRouter(prefix="/api", tags=["evidence"])

_cache: dict[tuple[int, int, bool], BatchView] = {}
_lock = asyncio.Lock()

_EPOCH_SIZE_CAP = 4_000


def _view(summary: BatchSummary, *, model_available: bool) -> BatchView:
    report = calibrate([Prediction(p, s) for p, s in summary.predictions])
    arms = [
        ArmView(
            arm=arm.value,
            label=_ARM_LABELS[arm],
            cases=result.case_count,
            recovered_count=result.recovered_count,
            rate_bps=result.rate.point_bps,
            rate_ci_low_bps=result.rate.low_bps,
            rate_ci_high_bps=result.rate.high_bps,
            at_risk=Amount.of(result.at_risk),
            recovered=Amount.of(result.recovered),
            net_recovered=Amount.of(result.net_recovered),
            total_cost=Amount.of(result.total_cost),
            attempts=result.attempts,
            contacts=result.contacts,
            by_failure_class={k: list(v) for k, v in result.by_failure_class.items()},
        )
        for arm in ExperimentArm
        if (result := summary.arms.get(arm)) is not None
    ]
    comparisons = [
        ComparisonView(
            treatment=c.treatment.value,
            against=c.against.value,
            difference_bps=c.difference.point_bps,
            ci_low_bps=c.difference.low_bps,
            ci_high_bps=c.difference.high_bps,
            significant=c.significant,
            underpowered=c.underpowered,
            minimum_detectable_bps=c.minimum_detectable_bps,
            z_score=round(c.z_score, 3),
            net_difference=Amount.of(c.net_difference),
            verdict=_verdict(c),
        )
        for c in summary.comparisons
    ]
    return BatchView(
        seed=summary.seed,
        population_size=summary.population_size,
        case_count=summary.case_count,
        total_at_risk=Amount.of(summary.total_at_risk),
        model_available=model_available,
        arms=arms,
        comparisons=comparisons,
        unmapped_codes=summary.unmapped_codes,
        classified_deterministically=summary.classified_deterministically,
        classified_by_model=summary.classified_by_model,
        model_safety_events=summary.model_safety_events,
        calibration_verdict=report.verdict,
        calibration_buckets=[
            CalibrationBucketView(
                label=bucket.label,
                count=bucket.count,
                predicted_bps=bucket.mean_predicted_bps,
                observed_bps=bucket.observed_bps,
                gap_bps=bucket.gap_bps,
            )
            for bucket in report.buckets
        ],
        limitations=_limitations(summary, model_available=model_available),
    )


def _verdict(comparison: object) -> str:
    """State the result in words. Never leave significance to be inferred."""
    c = comparison  # local alias keeps the type checker quiet about the Protocol
    if c.significant:  # type: ignore[attr-defined]
        direction = "better" if c.difference.point_bps > 0 else "WORSE"  # type: ignore[attr-defined]
        return (
            f"Significantly {direction} than {c.against.value}: the 95% interval "  # type: ignore[attr-defined]
            "excludes zero."
        )
    if c.underpowered:  # type: ignore[attr-defined]
        return (
            "Not significant, and this batch was too small to detect an effect of "
            f"{c.minimum_detectable_bps / 100:.1f} points or less. No claim is made."  # type: ignore[attr-defined]
        )
    return "Not significant: the interval includes zero, so there is no evidence of a difference."


@router.get("/batch", response_model=BatchView)
async def batch(
    seed: int = Query(default=20260902, gt=0),
    size: int = Query(default=2_000, ge=100, le=_EPOCH_SIZE_CAP),
    with_model: bool = Query(
        default=False,
        description="Model the LLM classifier as available, so its contribution to "
        "recovery can be measured rather than asserted.",
    ),
) -> BatchView:
    key = (seed, size, with_model)
    async with _lock:
        cached = _cache.get(key)
        if cached is not None:
            return cached

        from anvil.api.state import CONSOLE_EPOCH

        population = build_population(seed=seed, size=size, now=CONSOLE_EPOCH)
        # An even split, because a 10% holdout on a few hundred cases gives
        # intervals too wide to conclude anything, and a dashboard that cannot
        # conclude anything is a dashboard that invites people to guess.
        world = World(population, split=EVEN_SPLIT, model_available=with_model)
        # Off the event loop. World.run_anvil drives the graph with
        # asyncio.run, which cannot nest inside the server's running loop, and
        # a batch is several seconds of CPU that has no business blocking
        # every other request anyway.
        outcomes = await asyncio.to_thread(world.run_batch)
        summary = summarise(outcomes, seed=seed, population_size=size)
        view = _view(summary, model_available=with_model)
        _cache[key] = view
        return view
