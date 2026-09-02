"""The append-only double-entry ledger.

Invariants 1, 2, 3 and 8 from ARCHITECTURE.md live here, and they are enforced
at three levels so that no single mistake can breach them:

* **Schema** -- entries carry an explicit direction and integer minor units;
  a check constraint forbids non-positive amounts.
* **Database** -- a migration installs Postgres rules that make ``UPDATE`` and
  ``DELETE`` on ``ledger_entries`` raise. Not a convention: a refusal.
* **Application** -- the posting service asserts debits equal credits inside the
  same transaction that writes them.

Balances are never stored. They are derived by summing entries, which means a
balance cannot drift from its history because there is nothing to drift.
"""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
from anvil.domain.enums import AccountKind, EntryDirection, LedgerTxnType
from anvil.domain.money import Currency, Money


class Account(Base, TimestampMixin, MerchantScopedMixin):
    """A node in the chart of accounts.

    ``code`` is the stable handle used in code (``"merchant:receivable"``,
    ``"merchant:concession_budget"``); ids are for foreign keys.
    """

    __tablename__ = "accounts"

    id: Mapped[str] = pk_column("acc")
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[AccountKind] = mapped_column(
        sa.Enum(AccountKind, native_enum=False, length=24), nullable=False
    )
    currency: Mapped[Currency] = mapped_column(CurrencyType, nullable=False, default=Currency.INR)
    #: Set for per-customer sub-accounts; null for merchant-level accounts.
    customer_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("customers.id", ondelete="RESTRICT"), index=True
    )
    description: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        sa.UniqueConstraint("merchant_id", "code", "customer_id", name="uq_account_scope"),
        Index("ix_accounts_merchant_kind", "merchant_id", "kind"),
    )

    @property
    def normal_direction(self) -> EntryDirection:
        """Assets and expenses increase on the debit side; the rest on the credit side."""
        if self.kind in (AccountKind.ASSET, AccountKind.EXPENSE, AccountKind.CONTRA_REVENUE):
            return EntryDirection.DEBIT
        return EntryDirection.CREDIT


class LedgerTransaction(Base, CreatedAtMixin, MerchantScopedMixin):
    """A balanced set of entries, committed atomically.

    Append-only. A mistake is corrected by posting a reversal that references
    this row through ``reverses_transaction_id``, never by editing it.
    """

    __tablename__ = "ledger_transactions"

    id: Mapped[str] = pk_column("ltx")
    txn_type: Mapped[LedgerTxnType] = mapped_column(
        sa.Enum(LedgerTxnType, native_enum=False, length=40), nullable=False, index=True
    )
    currency: Mapped[Currency] = mapped_column(CurrencyType, nullable=False, default=Currency.INR)

    #: Effective date for reporting, which may differ from ``created_at`` when a
    #: settlement lands late.
    effective_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, index=True)

    case_id: Mapped[str | None] = mapped_column(String(32), index=True)
    action_id: Mapped[str | None] = mapped_column(String(32), index=True)
    customer_id: Mapped[str | None] = mapped_column(String(32), index=True)

    #: Stable across retries of the same logical posting, so a replayed message
    #: cannot double-post. This is invariant 5 applied to our own ledger.
    idempotency_key: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)

    reverses_transaction_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("ledger_transactions.id", ondelete="RESTRICT"), unique=True
    )
    narration: Mapped[str] = mapped_column(Text, nullable=False)

    entries: Mapped[list[LedgerEntry]] = relationship(
        back_populates="transaction", lazy="selectin", cascade="all"
    )

    __table_args__ = (
        Index("ix_ledger_txn_merchant_effective", "merchant_id", "effective_at"),
        Index("ix_ledger_txn_case", "case_id", "created_at"),
    )

    @property
    def is_reversal(self) -> bool:
        return self.reverses_transaction_id is not None


class LedgerEntry(Base, CreatedAtMixin):
    """One side of one posting. Immutable, forever.

    ``amount_minor`` is always strictly positive; direction is carried
    explicitly rather than by sign, so a sign error cannot quietly turn a debit
    into a credit.
    """

    __tablename__ = "ledger_entries"

    id: Mapped[str] = pk_column("len")
    transaction_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("ledger_transactions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    account_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    direction: Mapped[EntryDirection] = mapped_column(
        sa.Enum(EntryDirection, native_enum=False, length=8), nullable=False
    )
    amount_minor: Mapped[int] = money_minor()
    currency: Mapped[Currency] = mapped_column(CurrencyType, nullable=False, default=Currency.INR)
    #: Position within the transaction, so replay is deterministically ordered.
    sequence: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=0)

    transaction: Mapped[LedgerTransaction] = relationship(back_populates="entries", lazy="raise")

    __table_args__ = (
        sa.CheckConstraint("amount_minor > 0", name="entry_amount_strictly_positive"),
        sa.UniqueConstraint("transaction_id", "sequence", name="uq_entry_txn_sequence"),
        Index("ix_ledger_entries_account_created", "account_id", "created_at"),
    )

    @property
    def amount(self) -> Money:
        return Money(self.amount_minor, self.currency)

    @property
    def signed_minor(self) -> int:
        """Debit positive, credit negative. For summing into a balance."""
        return self.amount_minor if self.direction is EntryDirection.DEBIT else -self.amount_minor


class BudgetReservation(Base, TimestampMixin, MerchantScopedMixin):
    """A hold against a merchant's concession budget.

    Invariant 8. A concession is reserved under ``SELECT … FOR UPDATE`` *before*
    the action executes and settled or released afterwards, so two cases running
    concurrently cannot jointly overspend a budget that only had room for one.
    The lock is taken on the budget row, not on this table.
    """

    __tablename__ = "budget_reservations"

    id: Mapped[str] = pk_column("rsv")
    budget_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("concession_budgets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    case_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    action_id: Mapped[str | None] = mapped_column(String(32), index=True)
    customer_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    amount_minor: Mapped[int] = money_minor()
    currency: Mapped[Currency] = mapped_column(CurrencyType, nullable=False, default=Currency.INR)

    #: ``held`` -> ``settled`` when the concession actually lands, or
    #: ``released`` when the action is rejected, expires or fails.
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="held", index=True)
    expires_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    settled_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    released_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    #: Stable across retries so a replayed action reuses its hold.
    idempotency_key: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)

    __table_args__ = (
        sa.CheckConstraint("amount_minor > 0", name="reservation_amount_positive"),
        sa.CheckConstraint(
            "state IN ('held','settled','released')", name="reservation_state_valid"
        ),
        Index("ix_reservations_budget_state", "budget_id", "state"),
    )

    @property
    def amount(self) -> Money:
        return Money(self.amount_minor, self.currency)

    @property
    def is_held(self) -> bool:
        return self.state == "held"


class ConcessionBudget(Base, TimestampMixin, MerchantScopedMixin):
    """The merchant-authorised pot the agent may concede from.

    ``funded_minor`` is the ceiling for the period. Available headroom is
    ``funded - settled - held``, all derived. This row is the lock target for
    concurrent reservations; it deliberately stores no computed balance.
    """

    __tablename__ = "concession_budgets"

    id: Mapped[str] = pk_column("bgt")
    period_start: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    period_end: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, index=True)

    funded_minor: Mapped[int] = money_minor()
    currency: Mapped[Currency] = mapped_column(CurrencyType, nullable=False, default=Currency.INR)

    #: Hard ceilings applied per concession, independent of headroom.
    per_customer_cap_minor: Mapped[int] = money_minor()
    per_action_cap_minor: Mapped[int] = money_minor()
    #: Maximum concession as a percentage of the subscription's monthly value.
    max_percent_of_mrr: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=25)

    __table_args__ = (
        sa.UniqueConstraint("merchant_id", "period_start", name="uq_budget_merchant_period"),
        sa.CheckConstraint("funded_minor >= 0", name="budget_funded_non_negative"),
        sa.CheckConstraint("per_customer_cap_minor > 0", name="budget_customer_cap_positive"),
        sa.CheckConstraint("per_action_cap_minor > 0", name="budget_action_cap_positive"),
        sa.CheckConstraint("max_percent_of_mrr BETWEEN 0 AND 100", name="budget_mrr_percent_range"),
        sa.CheckConstraint("period_end > period_start", name="budget_period_ordered"),
    )

    @property
    def funded(self) -> Money:
        return Money(self.funded_minor, self.currency)
