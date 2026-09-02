"""The concession budget: holds, settlement, release and expiry.

Invariant 8. Two recovery cases running concurrently must not be able to jointly
concede more than the merchant authorised, and the mechanism for that is a row
lock rather than an application-level check.

**Why the lock is on the budget row and not on the reservations.** Headroom is a
property of the *set* of reservations, so no lock on any individual reservation
can protect it -- two transactions could each insert a row that is fine on its
own and jointly overspend. Locking the single :class:`ConcessionBudget` row
serialises the read-compute-insert sequence for that budget, which is the
smallest thing that makes the arithmetic safe. It also means contention is
scoped to one merchant's budget rather than to the whole table.

**Why holds are not ledger transactions.** See the module docstring of
:mod:`anvil.ledger.posting`: a hold is not an economic event. Only the concession
that eventually settles reaches the books.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from anvil.core.errors import BudgetExhausted, NotFound, ValidationError
from anvil.core.ids import IdPrefix, new_id
from anvil.db.models.ledger import BudgetReservation, ConcessionBudget
from anvil.domain.money import Currency, Money

#: How long a hold survives without being settled. A hold that outlives its
#: action would silently shrink the budget forever, so every hold has a deadline
#: and :func:`expire_stale` reclaims the ones that pass it.
DEFAULT_HOLD_MINUTES = 120

# ---------------------------------------------------------------------------
# Pure arithmetic
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BudgetPosition:
    """A budget's funded total and what has been taken out of it.

    Separating ``settled`` from ``held`` matters: settled money is gone, held
    money is merely spoken for and may come back. Headroom subtracts both,
    because a hold that might be released is not headroom you can promise to
    someone else in the meantime.
    """

    funded: Money
    settled: Money
    held: Money

    @property
    def headroom(self) -> Money:
        return self.funded - self.settled - self.held

    @property
    def utilisation_bps(self) -> int:
        """How much of the budget is spoken for, in basis points."""
        if self.funded.is_zero:
            return 10_000 if not (self.settled + self.held).is_zero else 0
        used = (self.settled + self.held).minor
        return int(Decimal(used) * 10_000 / Decimal(self.funded.minor))


@dataclass(frozen=True, slots=True)
class ReservationRequest:
    """A proposed hold, with everything the caps are checked against."""

    budget_id: str
    merchant_id: str
    case_id: str
    customer_id: str
    amount: Money
    idempotency_key: str
    #: The subscription's monthly value, for the percent-of-MRR ceiling.
    subscription_mrr: Money
    action_id: str | None = None


@dataclass(frozen=True, slots=True)
class CapCheck:
    """The outcome of checking one proposed hold against every ceiling.

    Returns *why* it failed rather than a bare boolean, because the operator
    reading the console needs to know whether the agent was stopped by the
    per-action cap, the per-customer cap, the MRR ratio or plain headroom --
    those imply four different corrective actions.
    """

    allowed: bool
    reason: str | None = None
    limiting_cap: str | None = None
    permitted: Money | None = None

    @classmethod
    def ok(cls, permitted: Money) -> CapCheck:
        return cls(allowed=True, permitted=permitted)

    @classmethod
    def refused(cls, reason: str, cap: str) -> CapCheck:
        return cls(allowed=False, reason=reason, limiting_cap=cap)


def check_caps(
    *,
    request: ReservationRequest,
    position: BudgetPosition,
    per_action_cap: Money,
    per_customer_cap: Money,
    customer_already_conceded: Money,
    max_percent_of_mrr: int,
) -> CapCheck:
    """Every ceiling, checked in the order that gives the most useful refusal.

    Order is deliberate: the caps that describe a *policy* the merchant set are
    checked before plain headroom, so a refusal says "this exceeds your
    per-customer limit" rather than the much less actionable "the budget is
    nearly empty" when both happen to be true.
    """
    if not request.amount.is_positive:
        return CapCheck.refused("a concession must be a positive amount", "amount")

    if request.amount > per_action_cap:
        return CapCheck.refused(
            f"{request.amount} exceeds the per-action ceiling of {per_action_cap}",
            "per_action_cap",
        )

    customer_total = customer_already_conceded + request.amount
    if customer_total > per_customer_cap:
        return CapCheck.refused(
            f"{request.amount} would take this customer to {customer_total}, "
            f"past the per-customer ceiling of {per_customer_cap}",
            "per_customer_cap",
        )

    if not request.subscription_mrr.is_zero:
        mrr_ceiling = request.subscription_mrr.percent(max_percent_of_mrr)
        if request.amount > mrr_ceiling:
            return CapCheck.refused(
                f"{request.amount} is more than {max_percent_of_mrr}% of the "
                f"subscription's {request.subscription_mrr} monthly value "
                f"(ceiling {mrr_ceiling})",
                "max_percent_of_mrr",
            )

    if request.amount > position.headroom:
        return CapCheck.refused(
            f"{request.amount} exceeds the remaining budget headroom of {position.headroom}",
            "headroom",
        )

    return CapCheck.ok(request.amount)


# ---------------------------------------------------------------------------
# Session-backed layer. Everything above this line is pure.
# ---------------------------------------------------------------------------


async def _load_budget_locked(session: AsyncSession, budget_id: str) -> ConcessionBudget:
    """Load the budget with a row lock held for the rest of the transaction."""
    budget = await session.scalar(
        sa.select(ConcessionBudget).where(ConcessionBudget.id == budget_id).with_for_update()
    )
    if budget is None:
        raise NotFound(f"concession budget {budget_id} does not exist", budget_id=budget_id)
    return budget


async def position_of(session: AsyncSession, budget: ConcessionBudget) -> BudgetPosition:
    """Sum settled and held reservations against a budget."""
    rows = (
        await session.execute(
            sa.select(
                BudgetReservation.state,
                sa.func.coalesce(sa.func.sum(BudgetReservation.amount_minor), 0),
            )
            .where(BudgetReservation.budget_id == budget.id)
            .group_by(BudgetReservation.state)
        )
    ).all()
    totals = {state: int(amount) for state, amount in rows}
    return BudgetPosition(
        funded=Money(budget.funded_minor, budget.currency),
        settled=Money(totals.get("settled", 0), budget.currency),
        held=Money(totals.get("held", 0), budget.currency),
    )


async def _customer_conceded(session: AsyncSession, budget_id: str, customer_id: str) -> Money:
    """What this customer has already had held or settled against this budget."""
    total = await session.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(BudgetReservation.amount_minor), 0)).where(
            BudgetReservation.budget_id == budget_id,
            BudgetReservation.customer_id == customer_id,
            BudgetReservation.state.in_(("held", "settled")),
        )
    )
    return Money(int(total or 0), Currency.INR)


async def reserve(
    session: AsyncSession,
    request: ReservationRequest,
    *,
    now: dt.datetime,
    hold_minutes: int = DEFAULT_HOLD_MINUTES,
) -> BudgetReservation:
    """Take a hold, or raise :class:`BudgetExhausted` explaining exactly why not.

    The lock is taken first and held until the caller's transaction commits, so
    the headroom computed here cannot be invalidated by a concurrent reserve
    before this one's row is inserted.
    """
    existing = await session.scalar(
        sa.select(BudgetReservation).where(
            BudgetReservation.idempotency_key == request.idempotency_key
        )
    )
    if existing is not None:
        return existing

    budget = await _load_budget_locked(session, request.budget_id)
    if budget.currency is not request.amount.currency:
        raise ValidationError(
            "reservation currency does not match the budget",
            budget_currency=budget.currency.value,
            requested=request.amount.currency.value,
        )

    position = await position_of(session, budget)
    verdict = check_caps(
        request=request,
        position=position,
        per_action_cap=Money(budget.per_action_cap_minor, budget.currency),
        per_customer_cap=Money(budget.per_customer_cap_minor, budget.currency),
        customer_already_conceded=await _customer_conceded(session, budget.id, request.customer_id),
        max_percent_of_mrr=budget.max_percent_of_mrr,
    )
    if not verdict.allowed:
        raise BudgetExhausted(
            verdict.reason or "the concession was refused",
            budget_id=budget.id,
            case_id=request.case_id,
            customer_id=request.customer_id,
            limiting_cap=verdict.limiting_cap,
            requested=request.amount.minor,
            headroom=position.headroom.minor,
        )

    reservation = BudgetReservation(
        id=new_id(IdPrefix.RESERVATION),
        merchant_id=request.merchant_id,
        budget_id=budget.id,
        case_id=request.case_id,
        action_id=request.action_id,
        customer_id=request.customer_id,
        amount_minor=request.amount.minor,
        currency=request.amount.currency,
        state="held",
        expires_at=now + dt.timedelta(minutes=hold_minutes),
        idempotency_key=request.idempotency_key,
    )
    session.add(reservation)
    await session.flush()
    return reservation


async def settle(
    session: AsyncSession, reservation_id: str, *, now: dt.datetime
) -> BudgetReservation:
    """Convert a hold into spend. Idempotent: settling twice is a no-op."""
    reservation = await session.get(BudgetReservation, reservation_id, with_for_update=True)
    if reservation is None:
        raise NotFound(f"reservation {reservation_id} does not exist")
    if reservation.state == "settled":
        return reservation
    if reservation.state == "released":
        raise ValidationError(
            "a released reservation cannot be settled; take a fresh hold instead",
            reservation_id=reservation_id,
        )
    reservation.state = "settled"
    reservation.settled_at = now
    await session.flush()
    return reservation


async def release(
    session: AsyncSession, reservation_id: str, *, now: dt.datetime, reason: str = ""
) -> BudgetReservation:
    """Return a hold to the budget. Idempotent, and refuses to unspend money."""
    reservation = await session.get(BudgetReservation, reservation_id, with_for_update=True)
    if reservation is None:
        raise NotFound(f"reservation {reservation_id} does not exist")
    if reservation.state == "released":
        return reservation
    if reservation.state == "settled":
        raise ValidationError(
            "a settled concession cannot be released; post a reversal instead",
            reservation_id=reservation_id,
            reason=reason,
        )
    reservation.state = "released"
    reservation.released_at = now
    await session.flush()
    return reservation


async def expire_stale(session: AsyncSession, *, now: dt.datetime, limit: int = 500) -> int:
    """Release holds that outlived their deadline. Returns how many were freed.

    Without this a crashed worker would permanently shrink a merchant's budget by
    the size of whatever it was holding, and the shrinkage would be invisible --
    the budget would simply start refusing concessions for no stated reason.
    """
    stale = (
        await session.scalars(
            sa.select(BudgetReservation)
            .where(BudgetReservation.state == "held", BudgetReservation.expires_at <= now)
            .order_by(BudgetReservation.expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for reservation in stale:
        reservation.state = "released"
        reservation.released_at = now
    if stale:
        await session.flush()
    return len(stale)
