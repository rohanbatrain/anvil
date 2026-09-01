"""Batches, arm assignment and the recorded results of an experiment run.

Section 11 of ARCHITECTURE.md. The point of these tables is to make the claim
"we recovered X" survive the follow-up question "compared to what?".
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from anvil.db.base import (
    Base,
    CreatedAtMixin,
    CurrencyType,
    MerchantScopedMixin,
    TimestampMixin,
    UTCDateTime,
    money_minor,
    pk_column,
)
from anvil.domain.enums import ExperimentArm
from anvil.domain.money import Currency, Money


class RecoveryBatch(Base, TimestampMixin, MerchantScopedMixin):
    """A population of at-risk cases worked together under one experiment.

    The batch records its seed. Re-running with the same seed reproduces the
    same population, the same arm assignment and -- in offline mode -- the same
    outcomes, which is what makes the reported numbers checkable rather than
    merely claimed.
    """

    __tablename__ = "recovery_batches"

    id: Mapped[str] = pk_column("bat")
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    seed: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    #: Fractions in basis points; they must sum to 10000.
    control_bps: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=1000)
    baseline_bps: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=1000)
    anvil_bps: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=8000)

    case_count: Mapped[int] = mapped_column(nullable=False, default=0)
    total_at_risk_minor: Mapped[int] = money_minor()
    currency: Mapped[Currency] = mapped_column(CurrencyType, nullable=False, default=Currency.INR)

    started_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    #: Simulated horizon, so a 30-day recovery window runs in seconds.
    horizon_days: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=30)

    __table_args__ = (
        sa.CheckConstraint(
            "control_bps + baseline_bps + anvil_bps = 10000", name="batch_arms_sum_to_one"
        ),
        sa.CheckConstraint("seed > 0", name="batch_seed_positive"),
        sa.CheckConstraint("horizon_days > 0", name="batch_horizon_positive"),
    )

    @property
    def total_at_risk(self) -> Money:
        return Money(self.total_at_risk_minor, self.currency)


class ArmAssignment(Base, CreatedAtMixin):
    """Which arm a case landed in, and the hash that put it there.

    Assignment is a pure function of ``(batch seed, case id)``. Storing the hash
    alongside the arm means anyone can recompute the assignment and confirm it
    was not chosen after the fact to flatter the result.
    """

    __tablename__ = "arm_assignments"

    id: Mapped[str] = pk_column("asg")
    batch_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    arm: Mapped[ExperimentArm] = mapped_column(
        sa.Enum(ExperimentArm, native_enum=False, length=16), nullable=False, index=True
    )
    assignment_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Position of the hash in [0, 10000), for auditing the split.
    bucket: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False)

    __table_args__ = (
        Index("ix_assignments_batch_arm", "batch_id", "arm"),
        sa.CheckConstraint("bucket BETWEEN 0 AND 9999", name="assignment_bucket_range"),
    )


class BatchResult(Base, CreatedAtMixin):
    """Computed outcome for one arm of one batch.

    Stored rather than recomputed on every dashboard load, and stored *per arm*
    so lift is always a comparison of like with like. Confidence intervals are
    bootstrapped and persisted; when an interval crosses zero the dashboard says
    so instead of rounding the inconvenience away.
    """

    __tablename__ = "batch_results"

    id: Mapped[str] = pk_column("bres")
    batch_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    arm: Mapped[ExperimentArm] = mapped_column(
        sa.Enum(ExperimentArm, native_enum=False, length=16), nullable=False
    )

    case_count: Mapped[int] = mapped_column(nullable=False, default=0)
    recovered_count: Mapped[int] = mapped_column(nullable=False, default=0)
    at_risk_minor: Mapped[int] = money_minor()
    recovered_minor: Mapped[int] = money_minor()
    concession_minor: Mapped[int] = money_minor()
    channel_cost_minor: Mapped[int] = money_minor()
    model_cost_minor: Mapped[int] = money_minor()
    currency: Mapped[Currency] = mapped_column(CurrencyType, nullable=False, default=Currency.INR)

    #: Recovery rate in basis points, and its bootstrap interval.
    recovery_rate_bps: Mapped[int] = mapped_column(nullable=False, default=0)
    recovery_rate_ci_low_bps: Mapped[int] = mapped_column(nullable=False, default=0)
    recovery_rate_ci_high_bps: Mapped[int] = mapped_column(nullable=False, default=0)

    #: Populated for non-control arms only.
    lift_vs_control_bps: Mapped[int | None] = mapped_column()
    lift_ci_low_bps: Mapped[int | None] = mapped_column()
    lift_ci_high_bps: Mapped[int | None] = mapped_column()
    #: False when the interval straddles zero. Displayed honestly either way.
    is_significant: Mapped[bool | None] = mapped_column()

    #: Recovery broken out by failure class, so a headline number cannot hide a
    #: single class doing all the work.
    by_failure_class: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)

    __table_args__ = (
        sa.UniqueConstraint("batch_id", "arm", name="uq_result_batch_arm"),
        sa.CheckConstraint("recovered_count <= case_count", name="result_recovered_lte_cases"),
        sa.CheckConstraint("recovered_minor >= 0", name="result_recovered_non_negative"),
    )

    @property
    def net_recovered(self) -> Money:
        """Recovered, less every rupee it cost to recover it."""
        return Money(
            self.recovered_minor
            - self.concession_minor
            - self.channel_cost_minor
            - self.model_cost_minor,
            self.currency,
        )
