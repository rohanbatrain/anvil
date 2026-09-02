"""Deriving balances by summation. Never by storage.

Invariant 1: there is no stored balance anywhere in Anvil, and therefore nothing
that can drift away from the history that produced it. Every figure in this
module is a ``SUM`` over :class:`~anvil.db.models.ledger.LedgerEntry` rows.

The cost of this choice is real -- a balance is a scan rather than a lookup --
and it is worth paying at this scale for one reason: the failure mode of a
stored balance is silent. A cached balance that is wrong looks exactly like a
cached balance that is right, and you find out during an audit. A derived
balance cannot be wrong unless the entries are wrong, and the entries are
immutable. If this needed to scale past what a merchant-scoped index scan will
carry, the correct next step is a materialised rollup with the derivation kept
as the authority to check it against -- not a mutable balance column.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from anvil.core.errors import InvariantViolation
from anvil.db.models.ledger import Account, LedgerEntry, LedgerTransaction
from anvil.domain.enums import AccountKind, EntryDirection
from anvil.domain.money import Currency, Money
from anvil.ledger.accounts import AccountCode, AccountRef, normal_direction

# ---------------------------------------------------------------------------
# Pure arithmetic
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Balance:
    """An account's position, expressed in the direction it naturally grows.

    ``signed`` is raw debit-minus-credit. ``natural`` flips the sign for
    credit-natured accounts so that a healthy revenue account reads as a
    positive number rather than a negative one -- which is what an operator
    expects and what the console renders.
    """

    account: AccountRef
    debits: Money
    credits: Money

    @property
    def signed(self) -> Money:
        return self.debits - self.credits

    @property
    def natural(self) -> Money:
        if normal_direction(self.account.kind) is EntryDirection.DEBIT:
            return self.signed
        return -self.signed

    @property
    def is_negative_in_natural_terms(self) -> bool:
        """True when an account holds the opposite of what it should.

        Not automatically an error -- a receivable can legitimately go negative
        for an instant if a settlement is posted before its recognition -- but it
        is always worth surfacing, so the console flags it.
        """
        return self.natural.is_negative


def combine(balances: list[Balance]) -> tuple[Money, Money]:
    """Total debits and total credits across a set of balances."""
    if not balances:
        return Money.zero(), Money.zero()
    currency = balances[0].debits.currency
    debit_total = Money.zero(currency)
    credit_total = Money.zero(currency)
    for item in balances:
        debit_total = debit_total + item.debits
        credit_total = credit_total + item.credits
    return debit_total, credit_total


@dataclass(frozen=True, slots=True)
class TrialBalance:
    """Every account in a merchant's book, with the proof that it balances."""

    merchant_id: str
    currency: Currency
    balances: tuple[Balance, ...]
    as_of: dt.datetime | None = None

    @property
    def total_debits(self) -> Money:
        return combine(list(self.balances))[0]

    @property
    def total_credits(self) -> Money:
        return combine(list(self.balances))[1]

    @property
    def imbalance(self) -> Money:
        return self.total_debits - self.total_credits

    @property
    def balances_out(self) -> bool:
        return self.imbalance.is_zero

    def assert_balanced(self) -> None:
        """Raise if the books do not balance.

        Called by the invariant test suite and by the console's health check. If
        this ever fires in a running system, something has written entries
        outside :func:`anvil.ledger.posting.post`, and the correct response is to
        stop, not to reconcile.
        """
        if not self.balances_out:
            raise InvariantViolation(
                f"trial balance does not balance: debits and credits differ by {self.imbalance}",
                merchant_id=self.merchant_id,
                debits=self.total_debits.minor,
                credits=self.total_credits.minor,
            )

    def by_code(self, code: AccountCode) -> Money:
        """Natural-direction total across every account with this code.

        Sums the per-customer sub-accounts together with the control account, so
        ``by_code(CUSTOMER_RECEIVABLE)`` answers "how much are we chasing across
        all identified customers".
        """
        total = Money.zero(self.currency)
        for b in self.balances:
            if b.account.code is code:
                total = total + b.natural
        return total

    def by_kind(self, kind: AccountKind) -> Money:
        total = Money.zero(self.currency)
        for b in self.balances:
            if b.account.kind is kind:
                total = total + b.natural
        return total

    @property
    def net_revenue(self) -> Money:
        """Gross revenue less what was conceded to earn it."""
        return self.by_code(AccountCode.MERCHANT_REVENUE) - self.by_code(
            AccountCode.CONCESSIONS_GRANTED
        )

    @property
    def total_recovery_cost(self) -> Money:
        """Everything spent chasing the money: concessions, channels and model calls."""
        return (
            self.by_code(AccountCode.CONCESSIONS_GRANTED)
            + self.by_code(AccountCode.CHANNEL_EXPENSE)
            + self.by_code(AccountCode.MODEL_EXPENSE)
        )


# ---------------------------------------------------------------------------
# Session-backed layer. Everything above this line is pure.
# ---------------------------------------------------------------------------


async def balance(
    session: AsyncSession, account: AccountRef, *, as_of: dt.datetime | None = None
) -> Balance:
    """One account's balance, optionally as it stood at an instant.

    Point-in-time uses ``LedgerTransaction.effective_at`` rather than the entry's
    ``created_at``, because a settlement that lands late belongs to the day it
    settled economically, not the day the webhook arrived.
    """
    stmt = (
        sa.select(
            LedgerEntry.direction,
            sa.func.coalesce(sa.func.sum(LedgerEntry.amount_minor), 0),
        )
        .where(LedgerEntry.account_id == account.id)
        .group_by(LedgerEntry.direction)
    )
    if as_of is not None:
        stmt = stmt.join(
            LedgerTransaction, LedgerTransaction.id == LedgerEntry.transaction_id
        ).where(LedgerTransaction.effective_at <= as_of)

    debit_total = 0
    credit_total = 0
    for direction, total in (await session.execute(stmt)).all():
        if direction is EntryDirection.DEBIT or direction == EntryDirection.DEBIT.value:
            debit_total = int(total)
        else:
            credit_total = int(total)

    return Balance(
        account=account,
        debits=Money(debit_total, account.currency),
        credits=Money(credit_total, account.currency),
    )


async def balances_for(
    session: AsyncSession,
    merchant_id: str,
    currency: Currency = Currency.INR,
    *,
    as_of: dt.datetime | None = None,
) -> list[Balance]:
    """Every account in a merchant's chart, in one query rather than N.

    Accounts with no entries are included with a zero balance. Omitting them
    would make an empty account indistinguishable from a missing one, and
    "the concession budget account does not exist" is a very different problem
    from "the concession budget is empty".
    """
    entry_totals = sa.select(
        LedgerEntry.account_id.label("account_id"),
        sa.func.sum(
            sa.case(
                (LedgerEntry.direction == EntryDirection.DEBIT.value, LedgerEntry.amount_minor),
                else_=0,
            )
        ).label("debits"),
        sa.func.sum(
            sa.case(
                (
                    LedgerEntry.direction == EntryDirection.CREDIT.value,
                    LedgerEntry.amount_minor,
                ),
                else_=0,
            )
        ).label("credits"),
    ).group_by(LedgerEntry.account_id)
    if as_of is not None:
        entry_totals = entry_totals.join(
            LedgerTransaction, LedgerTransaction.id == LedgerEntry.transaction_id
        ).where(LedgerTransaction.effective_at <= as_of)
    totals = entry_totals.subquery()

    stmt = (
        sa.select(
            Account,
            sa.func.coalesce(totals.c.debits, 0),
            sa.func.coalesce(totals.c.credits, 0),
        )
        .outerjoin(totals, totals.c.account_id == Account.id)
        .where(Account.merchant_id == merchant_id, Account.currency == currency.value)
        .order_by(Account.code, Account.customer_id)
    )

    out: list[Balance] = []
    for account, debit_total, credit_total in (await session.execute(stmt)).all():
        ref = AccountRef(
            id=account.id,
            code=AccountCode(account.code),
            kind=account.kind,
            currency=account.currency,
            merchant_id=account.merchant_id,
            customer_id=account.customer_id,
        )
        out.append(
            Balance(
                account=ref,
                debits=Money(int(debit_total), currency),
                credits=Money(int(credit_total), currency),
            )
        )
    return out


async def trial_balance(
    session: AsyncSession,
    merchant_id: str,
    currency: Currency = Currency.INR,
    *,
    as_of: dt.datetime | None = None,
) -> TrialBalance:
    """The whole book, with the balance check available on the result."""
    return TrialBalance(
        merchant_id=merchant_id,
        currency=currency,
        balances=tuple(await balances_for(session, merchant_id, currency, as_of=as_of)),
        as_of=as_of,
    )
