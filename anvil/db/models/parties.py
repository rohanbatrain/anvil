"""Merchants, customers, plans and subscriptions -- the world Anvil recovers for."""

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
from anvil.domain.money import Currency, Money


class Merchant(Base, TimestampMixin):
    """A tenant. Owns its own policy bundle, concession budget and cases."""

    __tablename__ = "merchants"

    id: Mapped[str] = pk_column("mch")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(300))
    razorpay_account_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    currency: Mapped[Currency] = mapped_column(CurrencyType, nullable=False, default=Currency.INR)

    #: IST local hours during which no outreach may be sent. Enforced in policy.
    quiet_hours_start: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=21)
    quiet_hours_end: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=8)

    #: Governs whether the agent may act autonomously inside policy, or must
    #: queue every single action for a human. Merchants start in review-first.
    review_first: Mapped[bool] = mapped_column(nullable=False, default=True)

    active_policy_bundle_id: Mapped[str | None] = mapped_column(String(32))

    customers: Mapped[list[Customer]] = relationship(back_populates="merchant", lazy="raise")

    __table_args__ = (
        sa.CheckConstraint("quiet_hours_start BETWEEN 0 AND 23", name="quiet_start_range"),
        sa.CheckConstraint("quiet_hours_end BETWEEN 0 AND 23", name="quiet_end_range"),
    )


class Customer(Base, TimestampMixin, MerchantScopedMixin):
    """A data principal under the DPDPA. All contact details are tokenised.

    Anvil never stores a raw phone number, email address or VPA. It stores an
    irreversible pseudonym plus a display-safe hint (``"•••4821"``), which is
    enough for an operator to recognise the customer and never enough to
    contact them out of band or to leak them through a log.
    """

    __tablename__ = "customers"

    id: Mapped[str] = pk_column("cus")
    external_ref: Mapped[str | None] = mapped_column(String(64), index=True)

    email_token: Mapped[str | None] = mapped_column(String(64), index=True)
    email_hint: Mapped[str | None] = mapped_column(String(64))
    phone_token: Mapped[str | None] = mapped_column(String(64), index=True)
    phone_hint: Mapped[str | None] = mapped_column(String(32))
    vpa_token: Mapped[str | None] = mapped_column(String(64), index=True)
    vpa_hint: Mapped[str | None] = mapped_column(String(64))

    display_name: Mapped[str | None] = mapped_column(String(120))
    preferred_language: Mapped[str] = mapped_column(String(8), nullable=False, default="en")
    timezone: Mapped[str] = mapped_column(String(48), nullable=False, default="Asia/Kolkata")

    #: Denormalised behavioural features. Recomputed by the risk module; they
    #: are inputs to scoring and to the planner's context, never to the ledger.
    tenure_days: Mapped[int] = mapped_column(nullable=False, default=0)
    lifetime_value_minor: Mapped[int] = money_minor()
    prior_failures: Mapped[int] = mapped_column(nullable=False, default=0)
    prior_recoveries: Mapped[int] = mapped_column(nullable=False, default=0)
    prior_concessions_minor: Mapped[int] = money_minor()

    #: Set when a DPDPA erasure completes. Retained rows are tombstoned, not
    #: deleted, so the ledger stays whole.
    erased_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    merchant: Mapped[Merchant] = relationship(back_populates="customers", lazy="raise")
    subscriptions: Mapped[list[Subscription]] = relationship(
        back_populates="customer", lazy="raise"
    )

    __table_args__ = (
        Index("ix_customers_merchant_external", "merchant_id", "external_ref", unique=True),
        sa.CheckConstraint("lifetime_value_minor >= 0", name="ltv_non_negative"),
    )

    @property
    def lifetime_value(self) -> Money:
        return Money(self.lifetime_value_minor, Currency.INR)

    @property
    def is_erased(self) -> bool:
        return self.erased_at is not None


class Plan(Base, TimestampMixin, MerchantScopedMixin):
    """A priced subscription tier. Downgrades target a cheaper plan in the same family."""

    __tablename__ = "plans"

    id: Mapped[str] = pk_column("pln")
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    family: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tier_rank: Mapped[int] = mapped_column(nullable=False, default=0)
    amount_minor: Mapped[int] = money_minor()
    currency: Mapped[Currency] = mapped_column(CurrencyType, nullable=False, default=Currency.INR)
    interval: Mapped[str] = mapped_column(String(16), nullable=False, default="monthly")
    razorpay_plan_id: Mapped[str | None] = mapped_column(String(64), unique=True)

    __table_args__ = (sa.CheckConstraint("amount_minor > 0", name="plan_amount_positive"),)

    @property
    def amount(self) -> Money:
        return Money(self.amount_minor, self.currency)


class Subscription(Base, TimestampMixin, MerchantScopedMixin):
    """A recurring commitment, backed by exactly one active authorisation."""

    __tablename__ = "subscriptions"

    id: Mapped[str] = pk_column("sub")
    customer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    plan_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False
    )
    razorpay_subscription_id: Mapped[str | None] = mapped_column(String(64), unique=True)

    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active", index=True)
    started_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    current_period_start: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    current_period_end: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    #: Cached from the plan so a price change mid-cycle does not rewrite history.
    amount_minor: Mapped[int] = money_minor()
    currency: Mapped[Currency] = mapped_column(CurrencyType, nullable=False, default=Currency.INR)

    consecutive_failures: Mapped[int] = mapped_column(nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text)

    customer: Mapped[Customer] = relationship(back_populates="subscriptions", lazy="raise")
    plan: Mapped[Plan] = relationship(lazy="raise")

    __table_args__ = (
        Index("ix_subscriptions_merchant_status", "merchant_id", "status"),
        sa.CheckConstraint("amount_minor > 0", name="subscription_amount_positive"),
        sa.CheckConstraint("consecutive_failures >= 0", name="failures_non_negative"),
    )

    @property
    def amount(self) -> Money:
        return Money(self.amount_minor, self.currency)
