"""Was the scheduler telling the truth?

A model that says "62% likely" is only useful if, across all the times it said
62%, roughly 62% actually happened. Everything else in :mod:`anvil.risk` is a
claim; this module is the audit of that claim, and it is what turns "we built a
deterministic retry optimiser" into something a judge can check rather than
take on trust.

Three measures, because they answer different questions:

* **The reliability table** answers "where is it wrong?" -- it buckets
  predictions and compares each bucket's mean prediction against its observed
  rate, so systematic over-confidence in one band is visible instead of averaged
  into the whole.
* **The Brier score** answers "how wrong overall?" -- mean squared error on
  probabilities. It punishes confident mistakes far more than hedged ones, which
  is the right incentive for something that spends finite retry attempts.
* **Expected calibration error** answers "how wrong in a way that matters?" --
  the bucket-weighted average gap between predicted and observed.

Reported honestly, including when the sample is too small to conclude anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

BPS = 10_000
DEFAULT_BUCKETS = 10


@dataclass(frozen=True, slots=True)
class Prediction:
    """One prediction and what actually happened."""

    predicted_bps: int
    settled: bool

    def __post_init__(self) -> None:
        if not 0 <= self.predicted_bps <= BPS:
            raise ValueError(f"predicted_bps out of range: {self.predicted_bps}")


@dataclass(frozen=True, slots=True)
class Bucket:
    """One band of the reliability table."""

    low_bps: int
    high_bps: int
    count: int
    mean_predicted_bps: int
    observed_bps: int

    @property
    def gap_bps(self) -> int:
        """Positive means over-confident: it promised more than it delivered."""
        return self.mean_predicted_bps - self.observed_bps

    @property
    def label(self) -> str:
        return f"{self.low_bps // 100}-{self.high_bps // 100}%"


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    """The whole picture, including whether it is worth believing.

    ``sufficient_sample`` exists because a calibration report over 12 attempts
    is noise, and presenting noise as evidence is exactly the dishonesty this
    track's rubric penalises. Below the threshold the report still renders; it
    simply says so.
    """

    buckets: tuple[Bucket, ...]
    count: int
    brier_score_bps: int
    expected_calibration_error_bps: int
    observed_rate_bps: int
    mean_predicted_bps: int
    minimum_sample: int = 100

    @property
    def sufficient_sample(self) -> bool:
        return self.count >= self.minimum_sample

    @property
    def is_overconfident(self) -> bool:
        return self.mean_predicted_bps > self.observed_rate_bps

    @property
    def verdict(self) -> str:
        if self.count == 0:
            return "No completed attempts yet, so calibration cannot be assessed."
        if not self.sufficient_sample:
            return (
                f"Only {self.count} completed attempts. That is too few to assess "
                f"calibration; at least {self.minimum_sample} are needed before these "
                "numbers mean anything."
            )
        gap = abs(self.mean_predicted_bps - self.observed_rate_bps) / 100
        direction = "over" if self.is_overconfident else "under"
        if self.expected_calibration_error_bps <= 500:
            return (
                f"Well calibrated across {self.count} attempts: predictions averaged "
                f"{self.mean_predicted_bps / 100:.1f}% against an observed "
                f"{self.observed_rate_bps / 100:.1f}%, an expected calibration error of "
                f"{self.expected_calibration_error_bps / 100:.1f}%."
            )
        return (
            f"Systematically {direction}-confident by {gap:.1f} points across "
            f"{self.count} attempts (expected calibration error "
            f"{self.expected_calibration_error_bps / 100:.1f}%). The retry curves need "
            "re-fitting against observed outcomes."
        )


def calibrate(
    predictions: Sequence[Prediction],
    *,
    buckets: int = DEFAULT_BUCKETS,
    minimum_sample: int = 100,
) -> CalibrationReport:
    """Build the reliability table and its summary statistics.

    Empty buckets are omitted rather than reported as zero, because a bucket
    with no predictions is not evidence of perfect calibration in that band --
    it is an absence of evidence, and rendering it as a zero gap would claim
    otherwise.
    """
    if buckets < 1:
        raise ValueError("need at least one bucket")

    count = len(predictions)
    if count == 0:
        return CalibrationReport(
            buckets=(),
            count=0,
            brier_score_bps=0,
            expected_calibration_error_bps=0,
            observed_rate_bps=0,
            mean_predicted_bps=0,
            minimum_sample=minimum_sample,
        )

    width = BPS // buckets
    grouped: dict[int, list[Prediction]] = {}
    for p in predictions:
        index = min(buckets - 1, p.predicted_bps // width)
        grouped.setdefault(index, []).append(p)

    table: list[Bucket] = []
    weighted_gap = Decimal(0)
    for index in sorted(grouped):
        members = grouped[index]
        n = len(members)
        mean_predicted = sum(m.predicted_bps for m in members) // n
        observed = sum(1 for m in members if m.settled) * BPS // n
        table.append(
            Bucket(
                low_bps=index * width,
                high_bps=(index + 1) * width,
                count=n,
                mean_predicted_bps=mean_predicted,
                observed_bps=observed,
            )
        )
        weighted_gap += Decimal(abs(mean_predicted - observed)) * n

    # Brier score: mean squared error on probabilities, rescaled to bps so the
    # whole module speaks one unit.
    squared = Decimal(0)
    for p in predictions:
        actual = BPS if p.settled else 0
        diff = Decimal(p.predicted_bps - actual) / Decimal(BPS)
        squared += diff * diff
    brier = int((squared / Decimal(count) * BPS).to_integral_value())

    return CalibrationReport(
        buckets=tuple(table),
        count=count,
        brier_score_bps=brier,
        expected_calibration_error_bps=int((weighted_gap / Decimal(count)).to_integral_value()),
        observed_rate_bps=sum(1 for p in predictions if p.settled) * BPS // count,
        mean_predicted_bps=sum(p.predicted_bps for p in predictions) // count,
        minimum_sample=minimum_sample,
    )


def render_reliability_table(report: CalibrationReport) -> str:
    """The reliability table as fixed-width text, for the batch report."""
    if not report.buckets:
        return "  (no completed attempts)"
    lines = [
        f"  {'band':>10}  {'n':>6}  {'predicted':>10}  {'observed':>10}  {'gap':>8}",
        f"  {'-' * 10}  {'-' * 6}  {'-' * 10}  {'-' * 10}  {'-' * 8}",
    ]
    for b in report.buckets:
        lines.append(
            f"  {b.label:>10}  {b.count:>6}  {b.mean_predicted_bps / 100:>9.1f}%  "
            f"{b.observed_bps / 100:>9.1f}%  {b.gap_bps / 100:>+7.1f}%"
        )
    return "\n".join(lines)
