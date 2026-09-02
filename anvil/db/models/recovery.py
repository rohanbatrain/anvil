"""Recovery cases, proposed actions and the payment attempts they produce."""

from __future__ import annotations

import datetime as dt
from typing import Any

import sqlalchemy as sa
from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from anvil.db.base import (
    Base,
    CurrencyType,
    MerchantScopedMixin,
    TimestampMixin,
    UTCDateTime,
    money_minor,
    pk_column,
)
from anvil.domain.enums import (
    ActionStatus,
    ActionType,
    CaseStatus,
    ExperimentArm,
    FailureClass,
)
from anvil.domain.money import Currency, Money


class RecoveryCase(Base, TimestampMixin, MerchantScopedMixin):
    """One at-risk invoice, worked from failure to a terminal outcome.

    Exactly one LangGraph thread per case. ``thread_id`` is the join between the
    durable graph state and the relational read model, which is what lets the
    console show a live case and lets the worker resume one after a crash.
    """

    __tablename__ = "recovery_cases"

    id: Mapped[str] = pk_column("cse")
    customer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    subscription_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("subscriptions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    authorisation_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("authorisations.id", ondelete="RESTRICT"), index=True
    )
    batch_id: Mapped[str | None] = mapped_column(String(32), index=True)

    status: Mapped[CaseStatus] = mapped_column(
        sa.Enum(CaseStatus, native_enum=False, length=32),
        nullable=False,
        default=CaseStatus.OPEN,
        index=True,
    )
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    # --- the money at stake --------------------------------------------------
    amount_at_risk_minor: Mapped[int] = money_minor()
    amount_recovered_minor: Mapped[int] = money_minor()
    concession_granted_minor: Mapped[int] = money_minor()
    currency: Mapped[Currency] = mapped_column(CurrencyType, nullable=False, default=Currency.INR)

    # --- the failure ---------------------------------------------------------
    original_failure_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, nullable=False, index=True
    )
    raw_failure_code: Mapped[str | None] = mapped_column(String(64))
    raw_failure_description: Mapped[str | None] = mapped_column(Text)
    failure_class: Mapped[FailureClass | None] = mapped_column(
        sa.Enum(FailureClass, native_enum=False, length=32), index=True
    )
    #: True when the deterministic table resolved it; false when the LLM did.
    classified_deterministically: Mapped[bool | None] = mapped_column()

    # --- scoring -------------------------------------------------------------
    #: 0-1000 integer scores. Integers, so they sort and compare exactly.
    recovery_likelihood: Mapped[int | None] = mapped_column(sa.SmallInteger)
    churn_risk: Mapped[int | None] = mapped_column(sa.SmallInteger)
    priority_score: Mapped[int | None] = mapped_column(sa.Integer, index=True)

    # --- experiment ----------------------------------------------------------
    experiment_arm: Mapped[ExperimentArm | None] = mapped_column(
        sa.Enum(ExperimentArm, native_enum=False, length=16), index=True
    )

    # --- lifecycle -----------------------------------------------------------
    attempts_made: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=0)
    contacts_made: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=0)
    next_action_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, index=True)
    closed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    closure_reason: Mapped[str | None] = mapped_column(Text)

    #: The model's current working diagnosis, kept for display and audit. It is
    #: advisory: nothing downstream trusts it without a policy pass.
    diagnosis: Mapped[dict[str, Any] | None] = mapped_column()

    actions: Mapped[list[RecoveryAction]] = relationship(back_populates="case", lazy="raise")

    __table_args__ = (
        Index("ix_cases_merchant_status_priority", "merchant_id", "status", "priority_score"),
        Index("ix_cases_due", "status", "next_action_at"),
        Index("ix_cases_batch_arm", "batch_id", "experiment_arm"),
        sa.CheckConstraint("amount_at_risk_minor > 0", name="case_at_risk_positive"),
        sa.CheckConstraint("amount_recovered_minor >= 0", name="case_recovered_non_negative"),
        sa.CheckConstraint("concession_granted_minor >= 0", name="case_concession_non_negative"),
        sa.CheckConstraint(
            "recovery_likelihood IS NULL OR recovery_likelihood BETWEEN 0 AND 1000",
            name="case_likelihood_range",
        ),
        sa.CheckConstraint(
            "churn_risk IS NULL OR churn_risk BETWEEN 0 AND 1000", name="case_churn_range"
        ),
        sa.CheckConstraint("attempts_made >= 0", name="case_attempts_non_negative"),
        sa.CheckConstraint("contacts_made >= 0", name="case_contacts_non_negative"),
    )

    @property
    def amount_at_risk(self) -> Money:
        return Money(self.amount_at_risk_minor, self.currency)

    @property
    def amount_recovered(self) -> Money:
        return Money(self.amount_recovered_minor, self.currency)

    @property
    def net_recovered(self) -> Money:
        """Recovered less what it cost in concessions. The number that matters."""
        return Money(self.amount_recovered_minor - self.concession_granted_minor, self.currency)


class RecoveryAction(Base, TimestampMixin, MerchantScopedMixin):
    """One proposed step, from proposal through authorisation, policy and outcome.

    Every action carries the *evidence for its own legitimacy*: which
    authorisation permitted it, which policy rule allowed it, who approved it.
    An action row is therefore self-justifying -- reading it tells you why it
    was allowed to happen, without joining to anything.
    """

    __tablename__ = "recovery_actions"

    id: Mapped[str] = pk_column("act")
    case_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("recovery_cases.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=0)

    action_type: Mapped[ActionType] = mapped_column(
        sa.Enum(ActionType, native_enum=False, length=40), nullable=False, index=True
    )
    status: Mapped[ActionStatus] = mapped_column(
        sa.Enum(ActionStatus, native_enum=False, length=32),
        nullable=False,
        default=ActionStatus.PROPOSED,
        index=True,
    )

    amount_minor: Mapped[int | None] = mapped_column(sa.BigInteger)
    currency: Mapped[Currency] = mapped_column(CurrencyType, nullable=False, default=Currency.INR)
    payload: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)

    #: Why the planner proposed this. Shown verbatim to the approving operator --
    #: an operator approving an action they cannot see the reasoning for is not
    #: meaningfully in the loop.
    rationale: Mapped[str | None] = mapped_column(Text)
    model_confidence: Mapped[int | None] = mapped_column(sa.SmallInteger)

    # --- the legitimacy trail -------------------------------------------------
    authorisation_id: Mapped[str | None] = mapped_column(String(32), index=True)
    authorisation_decision: Mapped[str | None] = mapped_column(String(32))
    denial_reason: Mapped[str | None] = mapped_column(String(48))
    policy_bundle_id: Mapped[str | None] = mapped_column(String(32))
    policy_rule_id: Mapped[str | None] = mapped_column(String(32))
    policy_effect: Mapped[str | None] = mapped_column(String(24))
    approval_id: Mapped[str | None] = mapped_column(String(32), index=True)
    reservation_id: Mapped[str | None] = mapped_column(String(32), index=True)

    #: Stable across retries of this logical action. Invariant 5.
    idempotency_key: Mapped[str | None] = mapped_column(String(96), unique=True)

    scheduled_for: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, index=True)
    executed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    outcome_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    outcome_detail: Mapped[dict[str, Any] | None] = mapped_column()

    #: Expected value at proposal time, for scheduler calibration reporting.
    expected_recovery_minor: Mapped[int | None] = mapped_column(sa.BigInteger)
    expected_probability_bps: Mapped[int | None] = mapped_column(sa.SmallInteger)

    case: Mapped[RecoveryCase] = relationship(back_populates="actions", lazy="raise")

    __table_args__ = (
        sa.UniqueConstraint("case_id", "sequence", name="uq_action_case_sequence"),
        Index("ix_actions_due", "status", "scheduled_for"),
        Index("ix_actions_merchant_status", "merchant_id", "status"),
        sa.CheckConstraint(
            "amount_minor IS NULL OR amount_minor > 0", name="action_amount_positive"
        ),
        sa.CheckConstraint(
            "model_confidence IS NULL OR model_confidence BETWEEN 0 AND 100",
            name="action_confidence_range",
        ),
        sa.CheckConstraint(
            "expected_probability_bps IS NULL OR expected_probability_bps BETWEEN 0 AND 10000",
            name="action_probability_range",
        ),
    )

    @property
    def amount(self) -> Money | None:
        return None if self.amount_minor is None else Money(self.amount_minor, self.currency)


class PaymentAttempt(Base, TimestampMixin, MerchantScopedMixin):
    """A concrete debit attempt against the gateway.

    Separate from :class:`RecoveryAction` because one action can produce several
    attempts -- a split debit is one action and three attempts -- and because an
    attempt has its own gateway identity and its own unknown-outcome path.
    """

    __tablename__ = "payment_attempts"

    id: Mapped[str] = pk_column("atm")
    case_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    action_id: Mapped[str | None] = mapped_column(String(32), index=True)
    subscription_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    authorisation_id: Mapped[str | None] = mapped_column(String(32), index=True)

    attempt_number: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=1)
    amount_minor: Mapped[int] = money_minor()
    currency: Mapped[Currency] = mapped_column(CurrencyType, nullable=False, default=Currency.INR)

    idempotency_key: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    razorpay_order_id: Mapped[str | None] = mapped_column(String(64), index=True)

    requested_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    settled_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    succeeded: Mapped[bool | None] = mapped_column(index=True)
    raw_failure_code: Mapped[str | None] = mapped_column(String(64), index=True)
    raw_failure_description: Mapped[str | None] = mapped_column(Text)
    failure_class: Mapped[FailureClass | None] = mapped_column(
        sa.Enum(FailureClass, native_enum=False, length=32), index=True
    )

    #: True while the gateway outcome is genuinely unknown. A reconciler polls
    #: with the same idempotency key rather than blindly retrying.
    needs_reconciliation: Mapped[bool] = mapped_column(nullable=False, default=False, index=True)
    reconciled_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    #: What the scheduler predicted, so calibration can be measured honestly.
    predicted_probability_bps: Mapped[int | None] = mapped_column(sa.SmallInteger)

    __table_args__ = (
        Index("ix_attempts_case_number", "case_id", "attempt_number"),
        Index("ix_attempts_unreconciled", "needs_reconciliation", "requested_at"),
        sa.CheckConstraint("amount_minor > 0", name="attempt_amount_positive"),
        sa.CheckConstraint("attempt_number > 0", name="attempt_number_positive"),
    )

    @property
    def amount(self) -> Money:
        return Money(self.amount_minor, self.currency)
