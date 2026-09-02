"""Constructing and committing balanced double-entry transactions.

Invariants 2, 3 and 5 from ``docs/ARCHITECTURE.md`` are enforced here, and the
module is deliberately split so that they are enforced *before* anything
touches a database:

* Everything above the "session-backed layer" comment is pure. A
  :class:`TransactionDraft` is built from :class:`AccountRef` values and
  validated with no session, no I/O and no clock of its own. That is what lets
  the balance check be property-tested exhaustively.
* :func:`post` is the only function that writes, and the first thing it does is
  validate. There is no path from a caller to ``session.add`` that skips the
  check.

**Why no ledger entries for reservations.** :class:`~anvil.domain.enums.LedgerTxnType`
carries ``CONCESSION_RESERVED`` and ``CONCESSION_RELEASED``, but this module
never posts them, and that is on purpose. A hold is not an economic event -- no
value has moved, and the merchant is neither richer nor poorer for it. Posting
holds would inflate the ledger with pairs that always net to zero and would make
the trial balance a worse description of reality, not a better one. Reservations
live in :mod:`anvil.ledger.reservations`, which tracks them as their own
first-class rows; only the eventual concession reaches the books.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from anvil.core.errors import UnbalancedTransaction, ValidationError
from anvil.core.ids import IdPrefix, new_id
from anvil.db.models.ledger import LedgerEntry, LedgerTransaction
from anvil.domain.enums import EntryDirection, LedgerTxnType
from anvil.domain.money import Currency, Money
from anvil.ledger.accounts import AccountCode, AccountRef, ChartOfAccounts

# ---------------------------------------------------------------------------
# Pure construction and validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntryDraft:
    """One side of one posting, before it has an id or a transaction.

    ``amount`` is always strictly positive; the side is carried by ``direction``.
    Sign is not used to encode direction anywhere in Anvil, because a sign error
    is invisible on inspection while a wrong ``EntryDirection`` is not.
    """

    account: AccountRef
    direction: EntryDirection
    amount: Money

    def __post_init__(self) -> None:
        if not self.amount.is_positive:
            raise ValidationError(
                "ledger entries must carry a strictly positive amount; "
                "direction, not sign, expresses which side the entry is on",
                account=self.account.label,
                amount=self.amount.minor,
            )
        if self.account.currency is not self.amount.currency:
            raise ValidationError(
                "entry currency does not match the account's currency",
                account=self.account.label,
                account_currency=self.account.currency.value,
                entry_currency=self.amount.currency.value,
            )

    @property
    def signed_minor(self) -> int:
        """Debit positive, credit negative. Only used for the balance check."""
        return self.amount.minor if self.direction is EntryDirection.DEBIT else -self.amount.minor


def debit(account: AccountRef, amount: Money) -> EntryDraft:
    return EntryDraft(account=account, direction=EntryDirection.DEBIT, amount=amount)


def credit(account: AccountRef, amount: Money) -> EntryDraft:
    return EntryDraft(account=account, direction=EntryDirection.CREDIT, amount=amount)


@dataclass(frozen=True, slots=True)
class TransactionDraft:
    """A complete, balanced posting, ready to be committed.

    ``idempotency_key`` is required rather than optional. Making it mandatory
    means there is no convenient path that skips it, which is the only reliable
    way to keep invariant 5 true across a codebase that will grow.
    """

    merchant_id: str
    txn_type: LedgerTxnType
    currency: Currency
    effective_at: dt.datetime
    narration: str
    idempotency_key: str
    entries: tuple[EntryDraft, ...]
    case_id: str | None = None
    action_id: str | None = None
    customer_id: str | None = None
    reverses_transaction_id: str | None = None

    @property
    def total_debits(self) -> Money:
        return Money(
            sum(e.amount.minor for e in self.entries if e.direction is EntryDirection.DEBIT),
            self.currency,
        )

    @property
    def total_credits(self) -> Money:
        return Money(
            sum(e.amount.minor for e in self.entries if e.direction is EntryDirection.CREDIT),
            self.currency,
        )

    @property
    def imbalance_minor(self) -> int:
        return sum(e.signed_minor for e in self.entries)


def validate(draft: TransactionDraft) -> TransactionDraft:
    """Refuse anything that would corrupt the books. Returns the draft unchanged.

    Raising here rather than returning a result object is deliberate: an
    unbalanced transaction is not a business outcome a caller might reasonably
    handle, it is a defect. The only correct response is to abort the enclosing
    transaction, which an exception does and a return value invites you not to.
    """
    if not draft.entries:
        raise ValidationError("a transaction needs at least two entries", key=draft.idempotency_key)
    if len(draft.entries) < 2:
        raise ValidationError(
            "a single-entry transaction cannot balance", key=draft.idempotency_key
        )
    if not draft.idempotency_key:
        raise ValidationError("idempotency_key is required for every posting")
    if not draft.narration.strip():
        raise ValidationError("every posting must carry a narration a human can read")

    mismatched = [e for e in draft.entries if e.amount.currency is not draft.currency]
    if mismatched:
        raise ValidationError(
            "every entry must be in the transaction's currency; Anvil does not post "
            "cross-currency transactions without an explicit conversion leg",
            transaction_currency=draft.currency.value,
            offending=[e.account.label for e in mismatched],
        )

    wrong_merchant = [e for e in draft.entries if e.account.merchant_id != draft.merchant_id]
    if wrong_merchant:
        raise ValidationError(
            "a transaction may not span merchants",
            merchant_id=draft.merchant_id,
            offending=[e.account.label for e in wrong_merchant],
        )

    imbalance = draft.imbalance_minor
    if imbalance != 0:
        raise UnbalancedTransaction(
            f"debits and credits differ by {Money(abs(imbalance), draft.currency)}",
            key=draft.idempotency_key,
            txn_type=draft.txn_type.value,
            debits=draft.total_debits.minor,
            credits=draft.total_credits.minor,
            imbalance=imbalance,
        )
    return draft


def reverse_draft(
    original: TransactionDraft,
    original_id: str,
    *,
    effective_at: dt.datetime,
    idempotency_key: str,
    reason: str,
) -> TransactionDraft:
    """Mirror a posting so its net effect becomes zero.

    A correction is never an edit. The original row stays exactly as it was
    written and a second, opposite transaction points back at it, so the history
    shows both that a mistake was made and that it was fixed -- which is the
    information an auditor actually wants.
    """
    flipped = tuple(
        replace(
            e,
            direction=(
                EntryDirection.CREDIT
                if e.direction is EntryDirection.DEBIT
                else EntryDirection.DEBIT
            ),
        )
        for e in original.entries
    )
    return TransactionDraft(
        merchant_id=original.merchant_id,
        txn_type=LedgerTxnType.REVERSAL,
        currency=original.currency,
        effective_at=effective_at,
        narration=f"Reversal of {original_id}: {reason}",
        idempotency_key=idempotency_key,
        entries=flipped,
        case_id=original.case_id,
        action_id=original.action_id,
        customer_id=original.customer_id,
        reverses_transaction_id=original_id,
    )


# ---------------------------------------------------------------------------
# Builders -- one per economic event Anvil can cause
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PostingContext:
    """The identifiers every builder needs, gathered once so call sites stay short."""

    chart: ChartOfAccounts
    effective_at: dt.datetime
    case_id: str | None = None
    action_id: str | None = None
    customer_id: str | None = None
    extra_key_parts: tuple[str, ...] = field(default=())

    def key(self, *parts: str) -> str:
        from anvil.core.ids import idempotency_key

        return idempotency_key(
            self.chart.merchant_id,
            self.case_id or "-",
            self.action_id or "-",
            *self.extra_key_parts,
            *parts,
        )


def recognise_receivable(ctx: PostingContext, amount: Money) -> TransactionDraft:
    """The invoice exists and has not been paid.

    Posted when a recovery case opens. Doing this up front is what lets a later
    write-off reduce a real asset instead of being a memo nobody can reconcile,
    and it makes "how much are we currently chasing?" a balance rather than a
    query over case rows.
    """
    receivable = ctx.chart.receivable_for(ctx.customer_id)
    revenue = ctx.chart.ref(AccountCode.MERCHANT_REVENUE)
    return validate(
        TransactionDraft(
            merchant_id=ctx.chart.merchant_id,
            txn_type=LedgerTxnType.RECEIVABLE_RECOGNISED,
            currency=amount.currency,
            effective_at=ctx.effective_at,
            narration=f"Receivable recognised, {amount} at risk",
            idempotency_key=ctx.key("recognise", str(amount.minor)),
            entries=(debit(receivable, amount), credit(revenue, amount)),
            case_id=ctx.case_id,
            action_id=ctx.action_id,
            customer_id=ctx.customer_id,
        )
    )


def settle_recovered_debit(ctx: PostingContext, amount: Money) -> TransactionDraft:
    """A previously failed debit has cleared. This is the money the agent recovered."""
    cash = ctx.chart.ref(AccountCode.MERCHANT_CASH)
    receivable = ctx.chart.receivable_for(ctx.customer_id)
    return validate(
        TransactionDraft(
            merchant_id=ctx.chart.merchant_id,
            txn_type=LedgerTxnType.MANDATE_DEBIT_SETTLED,
            currency=amount.currency,
            effective_at=ctx.effective_at,
            narration=f"Recovered {amount} on a previously failed mandate debit",
            idempotency_key=ctx.key("settle", str(amount.minor)),
            entries=(debit(cash, amount), credit(receivable, amount)),
            case_id=ctx.case_id,
            action_id=ctx.action_id,
            customer_id=ctx.customer_id,
        )
    )


def fund_budget(ctx: PostingContext, amount: Money) -> TransactionDraft:
    """The merchant earmarks cash the agent is permitted to concede from.

    Holding the authorisation as a restricted asset rather than as a number in a
    config file is what makes overspending a *ledger* impossibility rather than
    a policy aspiration.
    """
    budget = ctx.chart.ref(AccountCode.CONCESSION_BUDGET)
    cash = ctx.chart.ref(AccountCode.MERCHANT_CASH)
    return validate(
        TransactionDraft(
            merchant_id=ctx.chart.merchant_id,
            txn_type=LedgerTxnType.BUDGET_FUNDED,
            currency=amount.currency,
            effective_at=ctx.effective_at,
            narration=f"Concession budget funded with {amount}",
            idempotency_key=ctx.key("fund_budget", str(amount.minor)),
            entries=(debit(budget, amount), credit(cash, amount)),
            case_id=ctx.case_id,
        )
    )


def grant_concession(ctx: PostingContext, amount: Money) -> TransactionDraft:
    """Forgive part of what is owed, drawing on the earmarked budget.

    Four legs, because two distinct things happen and collapsing them would hide
    one of them. Economically a concession costs *revenue*, not cash -- the
    merchant never pays anybody, they simply agree to receive less -- so the cost
    lands in contra-revenue and the receivable falls. Separately, the earmark
    that authorised it is consumed and the restricted cash returns to general
    cash. Netting these into two legs would make it impossible to answer "how
    much of the authorised budget has been used?" from the ledger alone.
    """
    concessions = ctx.chart.ref(AccountCode.CONCESSIONS_GRANTED)
    receivable = ctx.chart.receivable_for(ctx.customer_id)
    cash = ctx.chart.ref(AccountCode.MERCHANT_CASH)
    budget = ctx.chart.ref(AccountCode.CONCESSION_BUDGET)
    return validate(
        TransactionDraft(
            merchant_id=ctx.chart.merchant_id,
            txn_type=LedgerTxnType.CONCESSION_GRANTED,
            currency=amount.currency,
            effective_at=ctx.effective_at,
            narration=f"Concession of {amount} granted against the authorised budget",
            idempotency_key=ctx.key("concession", str(amount.minor)),
            entries=(
                debit(concessions, amount),
                credit(receivable, amount),
                debit(cash, amount),
                credit(budget, amount),
            ),
            case_id=ctx.case_id,
            action_id=ctx.action_id,
            customer_id=ctx.customer_id,
        )
    )


def record_channel_cost(ctx: PostingContext, amount: Money, channel: str) -> TransactionDraft:
    """What an outreach send cost. Feeds cost-per-recovered-rupee."""
    expense = ctx.chart.ref(AccountCode.CHANNEL_EXPENSE)
    cash = ctx.chart.ref(AccountCode.MERCHANT_CASH)
    return validate(
        TransactionDraft(
            merchant_id=ctx.chart.merchant_id,
            txn_type=LedgerTxnType.CHANNEL_COST,
            currency=amount.currency,
            effective_at=ctx.effective_at,
            narration=f"Channel cost {amount} for a {channel} send",
            idempotency_key=ctx.key("channel_cost", channel, str(amount.minor)),
            entries=(debit(expense, amount), credit(cash, amount)),
            case_id=ctx.case_id,
            action_id=ctx.action_id,
            customer_id=ctx.customer_id,
        )
    )


def record_model_cost(ctx: PostingContext, amount: Money, model: str) -> TransactionDraft:
    """What the agent's thinking cost.

    Posting model spend to the same books as the recovered revenue is what makes
    the economics of the agent arguable rather than assumed: an agent that
    recovers a lakh by spending two is a bad agent, and the ledger will say so.
    """
    expense = ctx.chart.ref(AccountCode.MODEL_EXPENSE)
    cash = ctx.chart.ref(AccountCode.MERCHANT_CASH)
    return validate(
        TransactionDraft(
            merchant_id=ctx.chart.merchant_id,
            txn_type=LedgerTxnType.MODEL_COST,
            currency=amount.currency,
            effective_at=ctx.effective_at,
            narration=f"Model cost {amount} ({model})",
            idempotency_key=ctx.key("model_cost", model, str(amount.minor)),
            entries=(debit(expense, amount), credit(cash, amount)),
            case_id=ctx.case_id,
            action_id=ctx.action_id,
            customer_id=ctx.customer_id,
        )
    )


def write_off(ctx: PostingContext, amount: Money, reason: str) -> TransactionDraft:
    """Abandon recovery. The receivable recognised at case open is removed."""
    write_offs = ctx.chart.ref(AccountCode.WRITE_OFFS)
    receivable = ctx.chart.receivable_for(ctx.customer_id)
    return validate(
        TransactionDraft(
            merchant_id=ctx.chart.merchant_id,
            txn_type=LedgerTxnType.WRITE_OFF,
            currency=amount.currency,
            effective_at=ctx.effective_at,
            narration=f"Wrote off {amount}: {reason}",
            idempotency_key=ctx.key("write_off", str(amount.minor)),
            entries=(debit(write_offs, amount), credit(receivable, amount)),
            case_id=ctx.case_id,
            action_id=ctx.action_id,
            customer_id=ctx.customer_id,
        )
    )


# ---------------------------------------------------------------------------
# Session-backed layer. Everything above this line is pure.
# ---------------------------------------------------------------------------


async def post(session: AsyncSession, draft: TransactionDraft) -> LedgerTransaction:
    """Commit a draft, or return the transaction a previous identical call wrote.

    The duplicate check is a SELECT on the unique ``idempotency_key`` rather than
    a caught constraint violation, because a caller that posts twice is doing
    something legitimate -- retrying -- and should get the original transaction
    back rather than an exception to interpret. The unique constraint remains the
    real guarantee for the genuinely concurrent case; this is the fast path.
    """
    validate(draft)

    existing = await session.scalar(
        sa.select(LedgerTransaction).where(
            LedgerTransaction.idempotency_key == draft.idempotency_key
        )
    )
    if existing is not None:
        return existing

    txn = LedgerTransaction(
        id=new_id(IdPrefix.LEDGER_TXN),
        merchant_id=draft.merchant_id,
        txn_type=draft.txn_type,
        currency=draft.currency,
        effective_at=draft.effective_at,
        case_id=draft.case_id,
        action_id=draft.action_id,
        customer_id=draft.customer_id,
        idempotency_key=draft.idempotency_key,
        reverses_transaction_id=draft.reverses_transaction_id,
        narration=draft.narration,
    )
    session.add(txn)

    for sequence, entry in enumerate(draft.entries):
        session.add(
            LedgerEntry(
                id=new_id(IdPrefix.LEDGER_ENTRY),
                transaction_id=txn.id,
                account_id=entry.account.id,
                direction=entry.direction,
                amount_minor=entry.amount.minor,
                currency=entry.amount.currency,
                sequence=sequence,
            )
        )

    await session.flush()
    return txn


async def post_all(
    session: AsyncSession, drafts: Sequence[TransactionDraft]
) -> list[LedgerTransaction]:
    """Commit several drafts in one unit of work. All or none."""
    return [await post(session, draft) for draft in drafts]


async def reverse(
    session: AsyncSession,
    original_id: str,
    original: TransactionDraft,
    *,
    effective_at: dt.datetime,
    reason: str,
) -> LedgerTransaction:
    """Post the mirror image of an existing transaction."""
    from anvil.core.ids import idempotency_key

    draft = reverse_draft(
        original,
        original_id,
        effective_at=effective_at,
        idempotency_key=idempotency_key("reversal", original_id),
        reason=reason,
    )
    return await post(session, draft)
