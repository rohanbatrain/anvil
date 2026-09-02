"""The statistics behind "we recovered X" -- and behind "compared to what?".

Every function here is seeded, so an interval computed on one machine is the
interval computed on another. Nothing rounds an inconvenience away.

**Why the confidence interval is on the difference.** The tempting shortcut is
to compute an interval for each arm and check whether they overlap. That test is
wrong, and it is wrong in the direction that flatters you: two intervals can
overlap while the difference is clearly non-zero, and the overlap heuristic will
tell you there is no effect when there is. Worse, it can be run backwards to
claim significance from non-overlapping intervals at a confidence level nobody
chose. The difference has its own sampling distribution and that is what gets
bootstrapped here.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

BPS = 10_000
DEFAULT_RESAMPLES = 4_000
DEFAULT_CONFIDENCE = 95


@dataclass(frozen=True, slots=True)
class Interval:
    """A point estimate with its interval, all in basis points."""

    point_bps: int
    low_bps: int
    high_bps: int
    confidence: int = DEFAULT_CONFIDENCE

    @property
    def crosses_zero(self) -> bool:
        return self.low_bps <= 0 <= self.high_bps

    @property
    def width_bps(self) -> int:
        return self.high_bps - self.low_bps

    def format_percent(self) -> str:
        return (
            f"{self.point_bps / 100:+.1f}%  [{self.low_bps / 100:+.1f}, {self.high_bps / 100:+.1f}]"
        )


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    position = q * (len(sorted_values) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return sorted_values[int(position)]
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def bootstrap_proportion(
    successes: int,
    trials: int,
    *,
    seed: int,
    resamples: int = DEFAULT_RESAMPLES,
    confidence: int = DEFAULT_CONFIDENCE,
) -> Interval:
    """A percentile bootstrap interval for one arm's recovery rate."""
    if trials <= 0:
        return Interval(0, 0, 0, confidence)
    observed = [1] * successes + [0] * (trials - successes)
    rng = random.Random(seed)
    draws = sorted(
        sum(observed[rng.randrange(trials)] for _ in range(trials)) / trials
        for _ in range(resamples)
    )
    tail = (100 - confidence) / 200
    return Interval(
        point_bps=round(successes / trials * BPS),
        low_bps=round(_percentile(draws, tail) * BPS),
        high_bps=round(_percentile(draws, 1 - tail) * BPS),
        confidence=confidence,
    )


def bootstrap_difference(
    treatment_successes: int,
    treatment_trials: int,
    control_successes: int,
    control_trials: int,
    *,
    seed: int,
    resamples: int = DEFAULT_RESAMPLES,
    confidence: int = DEFAULT_CONFIDENCE,
) -> Interval:
    """The interval for the *difference* in rates. The only correct comparison."""
    if treatment_trials <= 0 or control_trials <= 0:
        return Interval(0, 0, 0, confidence)
    treatment = [1] * treatment_successes + [0] * (treatment_trials - treatment_successes)
    control = [1] * control_successes + [0] * (control_trials - control_successes)
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(resamples):
        t = sum(treatment[rng.randrange(treatment_trials)] for _ in range(treatment_trials))
        c = sum(control[rng.randrange(control_trials)] for _ in range(control_trials))
        draws.append(t / treatment_trials - c / control_trials)
    draws.sort()
    tail = (100 - confidence) / 200
    point = treatment_successes / treatment_trials - control_successes / control_trials
    return Interval(
        point_bps=round(point * BPS),
        low_bps=round(_percentile(draws, tail) * BPS),
        high_bps=round(_percentile(draws, 1 - tail) * BPS),
        confidence=confidence,
    )


def two_proportion_z(
    treatment_successes: int,
    treatment_trials: int,
    control_successes: int,
    control_trials: int,
) -> float:
    """A closed-form cross-check on the bootstrap.

    If the two disagree materially, something is wrong with one of them, and
    having a second method is cheaper than trusting a single one.
    """
    if treatment_trials <= 0 or control_trials <= 0:
        return 0.0
    p1 = treatment_successes / treatment_trials
    p2 = control_successes / control_trials
    pooled = (treatment_successes + control_successes) / (treatment_trials + control_trials)
    denominator = math.sqrt(pooled * (1 - pooled) * (1 / treatment_trials + 1 / control_trials))
    return 0.0 if denominator == 0 else (p1 - p2) / denominator


def is_significant(difference: Interval) -> bool:
    """False whenever the interval admits no effect. No exceptions, no rounding."""
    return not difference.crosses_zero


def minimum_detectable_effect_bps(
    control_rate_bps: int, n_per_arm: int, *, power: float = 0.80
) -> int:
    """The smallest true difference this sample size could reliably detect.

    Reported alongside every non-significant result, because "we found no
    effect" and "this batch was too small to find one" are different claims and
    conflating them is how underpowered experiments get presented as evidence
    of absence.
    """
    if n_per_arm <= 1:
        return BPS
    z_alpha, z_beta = 1.96, 0.84 if power <= 0.80 else 1.28
    p = control_rate_bps / BPS
    variance = max(1e-9, 2 * p * (1 - p))
    effect = (z_alpha + z_beta) * math.sqrt(variance / n_per_arm)
    return round(min(1.0, effect) * BPS)
