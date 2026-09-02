"""The chart of accounts.

Anvil keeps its own books rather than reading numbers back out of the payment
gateway, because the question a merchant actually asks after a recovery batch --
"what did the agent bring back, what did it give away, and what did it cost to do
that?" -- is a double-entry question, and nothing short of a ledger answers it
without hand-waving.

Two decisions in here are worth stating plainly.

**Account ids are derived, not minted.** The id of an account is a blake2b digest
of ``(merchant, code, customer)``. Creating the chart is therefore idempotent
without a read-modify-write: the same tuple always yields the same primary key,
so a concurrent second call collides and does nothing. It also means a seeded
demo reproduces byte-identical account rows, which is what lets the reproducibility
claim in ``docs/ARCHITECTURE.md`` section 14 be literally true. The price is that
account ids no longer sort chronologically; nothing in the system orders accounts
by id, so that price is never actually paid.

**The receivable control account and the per-customer receivable sub-accounts are
alternatives, never both legs of one posting.** ``merchant:receivable`` carries
amounts Anvil is recovering that are not attributed to an identified customer;
``customer:receivable`` carries the ones that are. Debiting both for the same
rupee would count the same asset twice, so :meth:`ChartOfAccounts.receivable_for`
picks exactly one and every builder in :mod:`anvil.ledger.posting` goes through it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from anvil.core.errors import NotFound
from anvil.core.ids import IdPrefix, deterministic_id
from anvil.db.models.ledger import Account
from anvil.domain.enums import AccountKind, EntryDirection
from anvil.domain.money import Currency


class AccountCode(StrEnum):
    """The closed set of accounts Anvil is allowed to post to.

    Closed for the same reason :class:`~anvil.domain.enums.ActionType` is closed:
    a posting to an account nobody declared is a posting nobody can reconcile, and
    the whole value of the ledger is that every rupee lands somewhere a human
    already agreed it could land.
    """

    MERCHANT_RECEIVABLE = "merchant:receivable"
    MERCHANT_CASH = "merchant:cash"
    MERCHANT_REVENUE = "merchant:revenue"
    CONCESSION_BUDGET = "merchant:concession_budget"
    CONCESSIONS_GRANTED = "merchant:concessions_granted"
    CHANNEL_EXPENSE = "merchant:channel_expense"
    MODEL_EXPENSE = "merchant:model_expense"
    WRITE_OFFS = "merchant:write_offs"
    CUSTOMER_RECEIVABLE = "customer:receivable"


@dataclass(frozen=True, slots=True)
class AccountSpec:
    """The declaration of one account: what it is for and how it behaves.

    ``description`` is not decoration. It is the text an operator reads in the
    console next to a balance they are being asked to trust, so it explains the
    account's economic meaning rather than restating its name.
    """

    code: AccountCode
    kind: AccountKind
    name: str
    description: str
    #: True for accounts that exist once per customer rather than once per merchant.
    per_customer: bool = False


CHART: tuple[AccountSpec, ...] = (
    AccountSpec(
        code=AccountCode.MERCHANT_RECEIVABLE,
        kind=AccountKind.ASSET,
        name="Receivable (control)",
        description=(
            "Amounts owed to the merchant that Anvil is working to recover and which are "
            "not attributed to an identified customer. This is the control account; the "
            "per-customer sub-accounts are its subsidiary ledger. A single posting uses "
            "one or the other, never both, because both would count the asset twice."
        ),
    ),
    AccountSpec(
        code=AccountCode.MERCHANT_CASH,
        kind=AccountKind.ASSET,
        name="Cash",
        description=(
            "Settled funds. Increases when a recovered debit clears and decreases when "
            "the merchant earmarks money for concessions or pays for a channel send or a "
            "model call."
        ),
    ),
    AccountSpec(
        code=AccountCode.MERCHANT_REVENUE,
        kind=AccountKind.REVENUE,
        name="Subscription revenue",
        description=(
            "Revenue recognised on amounts that passed through Anvil. Gross of "
            "concessions: what the merchant would have earned had nothing been conceded. "
            "Net revenue is this less the contra-revenue account."
        ),
    ),
    AccountSpec(
        code=AccountCode.CONCESSION_BUDGET,
        kind=AccountKind.ASSET,
        name="Concession budget (earmarked)",
        description=(
            "Cash the merchant has explicitly set aside for the agent to concede from. "
            "Holding it as a restricted asset rather than as a policy number is what "
            "makes overspending a ledger impossibility rather than a rule someone can "
            "forget to check."
        ),
    ),
    AccountSpec(
        code=AccountCode.CONCESSIONS_GRANTED,
        kind=AccountKind.CONTRA_REVENUE,
        name="Concessions granted",
        description=(
            "The value the agent gave away to save a subscription: grace periods, "
            "partial payments, downgrades, winback discounts. Contra-revenue rather "
            "than expense, because a concession is a price reduction, not a purchase."
        ),
    ),
    AccountSpec(
        code=AccountCode.CHANNEL_EXPENSE,
        kind=AccountKind.EXPENSE,
        name="Channel cost",
        description=(
            "What outreach cost to send: SMS, WhatsApp, voice. Recorded per case so the "
            "evidence harness can report cost per recovered rupee honestly instead of "
            "quoting a recovery rate with the cost side left out."
        ),
    ),
    AccountSpec(
        code=AccountCode.MODEL_EXPENSE,
        kind=AccountKind.EXPENSE,
        name="Model cost",
        description=(
            "Inference spend attributable to a case. An agent that recovers a rupee for "
            "a rupee of tokens has recovered nothing, and the only way to notice that is "
            "to book it."
        ),
    ),
    AccountSpec(
        code=AccountCode.WRITE_OFFS,
        kind=AccountKind.EXPENSE,
        name="Write-offs",
        description=(
            "Receivables Anvil has given up on. Kept as its own expense line so an "
            "abandoned case is visible in the books rather than quietly disappearing "
            "from the recovery rate's denominator."
        ),
    ),
    AccountSpec(
        code=AccountCode.CUSTOMER_RECEIVABLE,
        kind=AccountKind.ASSET,
        name="Receivable",
        description=(
            "One customer's outstanding balance with this merchant. The subsidiary "
            "ledger behind the receivable control account; used whenever the case has an "
            "identified customer, which is nearly always."
        ),
        per_customer=True,
    ),
)

SPEC_BY_CODE: Mapping[AccountCode, AccountSpec] = {spec.code: spec for spec in CHART}

#: Accounts that exist exactly once per merchant.
MERCHANT_LEVEL_CODES: tuple[AccountCode, ...] = tuple(
    spec.code for spec in CHART if not spec.per_customer
)
#: Accounts that exist once per customer.
PER_CUSTOMER_CODES: tuple[AccountCode, ...] = tuple(
    spec.code for spec in CHART if spec.per_customer
)


def normal_direction(kind: AccountKind) -> EntryDirection:
    """The side an account of this kind increases on.

    Assets and expenses grow on the debit side; liabilities and revenue grow on
    the credit side. Contra-revenue sits with the debit group deliberately: it is
    a *reduction* of a credit-natured account, so it behaves like a debit even
    though it lives in the revenue section of the P&L.
    """
    if kind in (AccountKind.ASSET, AccountKind.EXPENSE, AccountKind.CONTRA_REVENUE):
        return EntryDirection.DEBIT
    return EntryDirection.CREDIT


def account_id_for(merchant_id: str, code: AccountCode, customer_id: str | None = None) -> str:
    """The deterministic primary key for one account.

    Derived rather than random so that ``ensure_accounts`` is idempotent through
    a primary-key collision instead of through a read-modify-write that two
    concurrent callers could interleave.
    """
    return deterministic_id(IdPrefix.ACCOUNT, merchant_id, code.value, customer_id or "")


@dataclass(frozen=True, slots=True)
class AccountRef:
    """A resolved account: enough to post to it, without holding an ORM row.

    The posting builders take these rather than :class:`~anvil.db.models.ledger.Account`
    instances so that constructing a balanced transaction needs no session, which
    is what makes the entry-construction logic testable with no database at all.
    """

    id: str
    code: AccountCode
    kind: AccountKind
    currency: Currency
    merchant_id: str
    customer_id: str | None = None

    @property
    def normal_direction(self) -> EntryDirection:
        return normal_direction(self.kind)

    @property
    def label(self) -> str:
        """``merchant:receivable`` or ``customer:receivable/cus_01J...``."""
        return f"{self.code.value}/{self.customer_id}" if self.customer_id else self.code.value


@dataclass(frozen=True, slots=True, eq=False)
class ChartOfAccounts:
    """One merchant's resolved chart. A pure lookup table, no I/O.

    Built either from the database (:func:`load_chart`, :func:`ensure_accounts`) or
    from arithmetic alone (:meth:`derive`). Because account ids are deterministic
    the two agree exactly, so a unit test can construct a chart with no session and
    still produce the ids the database would have produced.
    """

    merchant_id: str
    currency: Currency
    refs: Mapping[tuple[AccountCode, str | None], AccountRef]

    @classmethod
    def derive(
        cls,
        merchant_id: str,
        *,
        currency: Currency = Currency.INR,
        customer_ids: Sequence[str] = (),
    ) -> ChartOfAccounts:
        """Compute the chart the database would hold, without touching it."""
        refs: dict[tuple[AccountCode, str | None], AccountRef] = {}
        for spec in CHART:
            targets: tuple[str | None, ...] = tuple(customer_ids) if spec.per_customer else (None,)
            for customer_id in targets:
                refs[(spec.code, customer_id)] = AccountRef(
                    id=account_id_for(merchant_id, spec.code, customer_id),
                    code=spec.code,
                    kind=spec.kind,
                    currency=currency,
                    merchant_id=merchant_id,
                    customer_id=customer_id,
                )
        return cls(merchant_id=merchant_id, currency=currency, refs=refs)

    def ref(self, code: AccountCode, customer_id: str | None = None) -> AccountRef:
        """Look up one account, failing loudly rather than inventing it.

        A missing account means the chart was never created for this merchant or
        customer. Falling back to a nearby account would post real money to the
        wrong place, so this raises instead.
        """
        try:
            return self.refs[(code, customer_id if SPEC_BY_CODE[code].per_customer else None)]
        except KeyError:
            raise NotFound(
                f"account {code.value} is not in the chart for merchant {self.merchant_id}",
                merchant_id=self.merchant_id,
                code=code.value,
                customer_id=customer_id,
            ) from None

    def id_of(self, code: AccountCode, customer_id: str | None = None) -> str:
        return self.ref(code, customer_id).id

    def receivable_for(self, customer_id: str | None) -> AccountRef:
        """The single receivable account a posting for this customer should use.

        Resolves to the customer's sub-account when the case has an identified
        customer and that sub-account exists, and to the merchant control account
        otherwise. Exactly one of the two, always -- see the module docstring.
        """
        if customer_id is not None and (AccountCode.CUSTOMER_RECEIVABLE, customer_id) in self.refs:
            return self.refs[(AccountCode.CUSTOMER_RECEIVABLE, customer_id)]
        return self.ref(AccountCode.MERCHANT_RECEIVABLE)

    def has(self, code: AccountCode, customer_id: str | None = None) -> bool:
        key = (code, customer_id if SPEC_BY_CODE[code].per_customer else None)
        return key in self.refs

    def with_customers(self, customer_ids: Iterable[str]) -> ChartOfAccounts:
        """A copy extended with per-customer sub-accounts. Never mutates in place."""
        extended = dict(self.refs)
        for customer_id in customer_ids:
            for code in PER_CUSTOMER_CODES:
                extended[(code, customer_id)] = AccountRef(
                    id=account_id_for(self.merchant_id, code, customer_id),
                    code=code,
                    kind=SPEC_BY_CODE[code].kind,
                    currency=self.currency,
                    merchant_id=self.merchant_id,
                    customer_id=customer_id,
                )
        return ChartOfAccounts(merchant_id=self.merchant_id, currency=self.currency, refs=extended)

    def all_refs(self) -> tuple[AccountRef, ...]:
        """Every account in the chart, ordered so output is stable across runs."""
        return tuple(sorted(self.refs.values(), key=lambda r: (r.code.value, r.customer_id or "")))

    def __len__(self) -> int:
        return len(self.refs)


# ---------------------------------------------------------------------------
# Session-backed layer. Everything above this line is pure.
# ---------------------------------------------------------------------------


def _insert_values(ref: AccountRef) -> dict[str, object]:
    spec = SPEC_BY_CODE[ref.code]
    return {
        "id": ref.id,
        "merchant_id": ref.merchant_id,
        "code": ref.code.value,
        "name": spec.name,
        "kind": spec.kind,
        "currency": ref.currency,
        "customer_id": ref.customer_id,
        "description": spec.description,
    }


async def ensure_accounts(
    session: AsyncSession,
    merchant_id: str,
    *,
    currency: Currency = Currency.INR,
    customer_ids: Sequence[str] = (),
) -> ChartOfAccounts:
    """Create this merchant's chart if it is not already there, and return it.

    Idempotent by construction: ids are derived from the account's identity, so a
    repeat call conflicts on the primary key and the conflict is ignored. There is
    no read-then-write window for two concurrent callers to interleave in, which
    matters because the first posting of a busy batch may well be racing several
    others through this exact function.
    """
    chart = ChartOfAccounts.derive(merchant_id, currency=currency, customer_ids=customer_ids)
    rows = [_insert_values(ref) for ref in chart.all_refs()]
    await session.execute(pg_insert(Account).values(rows).on_conflict_do_nothing())
    return chart


async def load_chart(
    session: AsyncSession,
    merchant_id: str,
    *,
    customer_ids: Sequence[str] = (),
) -> ChartOfAccounts:
    """Read the chart that actually exists, rather than the one we assume exists.

    Codes that are not members of :class:`AccountCode` are skipped: another module
    is free to keep its own accounts, and this loader has no business failing
    because of them.
    """
    stmt = sa.select(Account).where(Account.merchant_id == merchant_id)
    if customer_ids:
        stmt = stmt.where(
            sa.or_(Account.customer_id.is_(None), Account.customer_id.in_(list(customer_ids)))
        )
    result = await session.execute(stmt)
    refs: dict[tuple[AccountCode, str | None], AccountRef] = {}
    currency = Currency.INR
    for account in result.scalars():
        try:
            code = AccountCode(account.code)
        except ValueError:
            continue
        currency = account.currency
        refs[(code, account.customer_id)] = AccountRef(
            id=account.id,
            code=code,
            kind=account.kind,
            currency=account.currency,
            merchant_id=account.merchant_id,
            customer_id=account.customer_id,
        )
    if not refs:
        raise NotFound(
            f"no chart of accounts for merchant {merchant_id}; call ensure_accounts first",
            merchant_id=merchant_id,
        )
    return ChartOfAccounts(merchant_id=merchant_id, currency=currency, refs=refs)


async def get_account(
    session: AsyncSession,
    merchant_id: str,
    code: AccountCode,
    customer_id: str | None = None,
) -> Account:
    """Fetch one account row. The typed accessor callers should reach for."""
    account_id = account_id_for(
        merchant_id, code, customer_id if SPEC_BY_CODE[code].per_customer else None
    )
    account = await session.get(Account, account_id)
    if account is None:
        raise NotFound(
            f"account {code.value} not found for merchant {merchant_id}",
            merchant_id=merchant_id,
            code=code.value,
            customer_id=customer_id,
        )
    return account
