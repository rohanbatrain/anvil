"""Authorisation objects: mandates, Reserve Pay blocks and delegated agent authority.

Section 8 of ARCHITECTURE.md. Every money-moving action must present one of
these and pass a structural check against it. The check is total -- there is no
branch that falls through to "allow" -- which is what makes ``bounded`` a
precondition of execution rather than a convention someone can forget.
"""

from __future__ import annotations

import datetime as dt

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
from anvil.domain.enums import AuthorisationStatus, AuthorisationType, InterruptKind
from anvil.domain.money import Currency, Money


class Authorisation(Base, TimestampMixin, MerchantScopedMixin):
    """A stored right to debit, of one of five kinds.

    The kinds share one table because the *check* is uniform -- amount, window,
    frequency, counterparty, remaining capacity -- and only the fields that
    matter differ. Keeping them together means there is exactly one code path
    that can say "authorised", which is the property worth protecting.
    """

    __tablename__ = "authorisations"

    id: Mapped[str] = pk_column("aut")
    customer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    subscription_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("subscriptions.id", ondelete="RESTRICT"), index=True
    )

    auth_type: Mapped[AuthorisationType] = mapped_column(
        sa.Enum(AuthorisationType, native_enum=False, length=32), nullable=False, index=True
    )
    status: Mapped[AuthorisationStatus] = mapped_column(
        sa.Enum(AuthorisationStatus, native_enum=False, length=32),
        nullable=False,
        default=AuthorisationStatus.ACTIVE,
        index=True,
    )

    #: Unique Mandate Number for UPI Autopay, UMRN for e-NACH, token ref for cards.
    external_reference: Mapped[str | None] = mapped_column(String(96), index=True)

    # --- limits -------------------------------------------------------------
    #: Ceiling for a single debit. Always set.
    max_amount_minor: Mapped[int] = money_minor()
    #: Ceiling across ``period_days``. Null means unlimited within the window.
    period_cap_minor: Mapped[int | None] = mapped_column(sa.BigInteger)
    period_days: Mapped[int | None] = mapped_column(sa.SmallInteger)
    currency: Mapped[Currency] = mapped_column(CurrencyType, nullable=False, default=Currency.INR)

    #: ``monthly``/``weekly``/``as_presented``. Governs frequency violations.
    frequency: Mapped[str] = mapped_column(String(24), nullable=False, default="monthly")
    #: Debit attempts permitted per billing cycle before the mandate is exhausted.
    max_attempts_per_cycle: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=3)

    valid_from: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    valid_until: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, index=True)

    # --- Reserve Pay (Single Block Multi Debit) -----------------------------
    #: Amount blocked up front. Debits draw it down; it can never go negative.
    blocked_amount_minor: Mapped[int | None] = mapped_column(sa.BigInteger)
    consumed_amount_minor: Mapped[int] = money_minor()

    # --- delegated agent authority (modelled on UPI Circle) ------------------
    #: Set when a principal delegated to a named agent rather than authorising
    #: the merchant directly. The agent's caps are checked *in addition to* the
    #: principal's, and the tighter of the two always wins.
    delegated_to_agent: Mapped[str | None] = mapped_column(String(64), index=True)
    agent_per_txn_cap_minor: Mapped[int | None] = mapped_column(sa.BigInteger)
    agent_period_cap_minor: Mapped[int | None] = mapped_column(sa.BigInteger)

    revoked_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    revocation_reason: Mapped[str | None] = mapped_column(Text)

    usages: Mapped[list[AuthorisationUsage]] = relationship(
        back_populates="authorisation", lazy="raise"
    )

    __table_args__ = (
        Index("ix_authorisations_customer_status", "customer_id", "status"),
        Index("ix_authorisations_sub_active", "subscription_id", "status"),
        sa.CheckConstraint("max_amount_minor > 0", name="max_amount_positive"),
        sa.CheckConstraint("consumed_amount_minor >= 0", name="consumed_non_negative"),
        sa.CheckConstraint(
            "blocked_amount_minor IS NULL OR consumed_amount_minor <= blocked_amount_minor",
            name="block_not_overdrawn",
        ),
        sa.CheckConstraint(
            "period_cap_minor IS NULL OR period_days IS NOT NULL", name="period_cap_needs_days"
        ),
        sa.CheckConstraint("max_attempts_per_cycle > 0", name="attempts_positive"),
    )

    # --- derived ------------------------------------------------------------

    @property
    def max_amount(self) -> Money:
        return Money(self.max_amount_minor, self.currency)

    @property
    def remaining_block(self) -> Money | None:
        """Undrawn portion of a Reserve Pay block, or None if not a block."""
        if self.blocked_amount_minor is None:
            return None
        return Money(self.blocked_amount_minor - self.consumed_amount_minor, self.currency)

    @property
    def is_block(self) -> bool:
        return self.auth_type is AuthorisationType.RESERVE_PAY

    @property
    def is_delegated(self) -> bool:
        return self.delegated_to_agent is not None

    def effective_per_txn_cap(self) -> Money:
        """The tighter of the principal's ceiling and the agent's delegated cap."""
        cap = self.max_amount
        if self.agent_per_txn_cap_minor is not None:
            cap = cap.min(Money(self.agent_per_txn_cap_minor, self.currency))
        return cap


class AuthorisationUsage(Base, TimestampMixin):
    """Consumption of an authorisation within one billing cycle.

    Rows are per ``(authorisation, cycle_start)``. Attempt counting and period
    spend live here rather than on the authorisation itself so that a cycle
    rollover is a new row, not a destructive reset -- the history of how much
    of a mandate was used, and when, survives.
    """

    __tablename__ = "authorisation_usages"

    id: Mapped[str] = pk_column("aus")
    authorisation_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("authorisations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cycle_start: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    cycle_end: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)

    attempts_used: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=0)
    amount_debited_minor: Mapped[int] = money_minor()
    last_attempt_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    authorisation: Mapped[Authorisation] = relationship(back_populates="usages", lazy="raise")

    __table_args__ = (
        sa.UniqueConstraint("authorisation_id", "cycle_start", name="uq_usage_auth_cycle"),
        sa.CheckConstraint("attempts_used >= 0", name="attempts_used_non_negative"),
        sa.CheckConstraint("amount_debited_minor >= 0", name="debited_non_negative"),
    )


class StepUpChallenge(Base, TimestampMixin, MerchantScopedMixin):
    """An Additional Factor of Authentication journey the graph is waiting on.

    Created when an action is within the principal's authority but outside the
    agent's delegated cap, or when the issuer demands AFA. The recovery graph
    interrupts and resumes only once this resolves -- the pause is real, not
    simulated away.
    """

    __tablename__ = "step_up_challenges"

    id: Mapped[str] = pk_column("stp")
    case_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    action_id: Mapped[str | None] = mapped_column(String(32), index=True)
    authorisation_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("authorisations.id", ondelete="RESTRICT"), nullable=False
    )
    customer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    kind: Mapped[InterruptKind] = mapped_column(
        sa.Enum(InterruptKind, native_enum=False, length=32),
        nullable=False,
        default=InterruptKind.AFA_STEP_UP,
    )
    method: Mapped[str] = mapped_column(String(24), nullable=False, default="otp")
    requested_amount_minor: Mapped[int] = money_minor()
    currency: Mapped[Currency] = mapped_column(CurrencyType, nullable=False, default=Currency.INR)

    #: Never the OTP itself -- a salted digest, so a database dump cannot be
    #: replayed to approve a debit.
    challenge_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    attempts: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=3)

    expires_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    succeeded: Mapped[bool | None] = mapped_column()

    #: LangGraph thread this challenge is blocking, so resolution can resume it.
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    __table_args__ = (
        Index("ix_stepup_pending", "merchant_id", "resolved_at"),
        sa.CheckConstraint("attempts >= 0 AND attempts <= max_attempts", name="stepup_attempts"),
        sa.CheckConstraint("requested_amount_minor > 0", name="stepup_amount_positive"),
    )

    @property
    def is_pending(self) -> bool:
        return self.resolved_at is None
