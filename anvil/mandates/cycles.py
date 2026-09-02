"""Billing-cycle arithmetic for authorisations.

An authorisation's limits are not absolute -- they are limits *per cycle*. The
attempt allowance resets each cycle and the period cap is measured across one.
So before anything can be checked, the cycle containing an instant has to be
derived, and it has to be derived the same way every time by every caller.

Cycles are anchored on ``valid_from`` rather than on the calendar. A mandate
registered on the 17th bills on the 17th, and its attempt allowance resets on
the 17th; pretending it resets on the 1st would silently hand the customer a
second allowance in their first month. Where a period cap is declared, the
cycle takes that period's length exactly, because a cap measured over a window
that is not the accounting window is not a cap at all.
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from typing import Final

from anvil.db.models.authorisation import Authorisation

#: "As presented" mandates place no ceiling on how often a debit may be raised.
#: They still need an accounting window, and a month is the conventional one.
AS_PRESENTED: Final = "as_presented"

#: Frequencies whose cycle is a whole number of calendar months. Calendar months,
#: not 30-day blocks: a monthly mandate taken out on 31 January is next due at
#: the end of February, which no fixed-length approximation gets right.
_MONTH_CYCLES: Final[dict[str, int]] = {
    "monthly": 1,
    AS_PRESENTED: 1,
    "bimonthly": 2,
    "quarterly": 3,
    "half_yearly": 6,
    "semi_annual": 6,
    "yearly": 12,
    "annual": 12,
}

#: Frequencies whose cycle is a fixed number of days.
_DAY_CYCLES: Final[dict[str, int]] = {
    "daily": 1,
    "weekly": 7,
    "fortnightly": 14,
    "biweekly": 14,
}

#: An unrecognised frequency string is treated as monthly and as *fixed*, never
#: as "as presented". Guessing generously in favour of more debits is the one
#: way this function could lose a customer money.
_FALLBACK_MONTHS: Final = 1


def is_as_presented(frequency: str) -> bool:
    """True when the mandate places no ceiling on presentation frequency.

    Only the literal ``as_presented`` qualifies. Anything unrecognised is held
    to the one-presentation-per-cycle rule, because failing closed on an
    unknown frequency costs at most a delayed debit, while failing open costs a
    duplicate one.
    """
    return frequency.strip().lower() == AS_PRESENTED


@dataclass(frozen=True, slots=True)
class CycleWindow:
    """A half-open ``[start, end)`` interval, plus its ordinal since ``valid_from``.

    Half-open so that consecutive cycles tile the timeline without an instant
    belonging to two of them -- which would let one debit be accounted against
    either of two allowances.
    """

    start: dt.datetime
    end: dt.datetime
    #: 0 for the cycle beginning at ``valid_from``; negative for instants before it.
    index: int

    def contains(self, at: dt.datetime) -> bool:
        return self.start <= _as_utc(at) < self.end

    @property
    def length(self) -> dt.timedelta:
        return self.end - self.start


def cycle_window(auth: Authorisation, at: dt.datetime) -> CycleWindow:
    """The cycle of ``auth`` containing ``at``.

    Where the authorisation declares ``period_days`` the cycle is exactly that
    long, so the period cap and the attempt allowance are measured over the same
    window. Otherwise the cycle comes from the declared frequency. Instants
    before ``valid_from`` return the notional cycle that would have preceded it,
    which keeps the function total -- the validity-window check, not this one,
    is what refuses a debit raised too early.
    """
    anchor = _as_utc(auth.valid_from)
    instant = _as_utc(at)

    if auth.period_days is not None:
        if auth.period_days < 1:
            raise ValueError(f"period_days must be >= 1, got {auth.period_days}")
        period = dt.timedelta(days=auth.period_days)
        index = (instant - anchor) // period
        start = anchor + period * index
        return CycleWindow(start=start, end=start + period, index=index)

    frequency = auth.frequency.strip().lower()
    if frequency in _DAY_CYCLES:
        period = dt.timedelta(days=_DAY_CYCLES[frequency])
        index = (instant - anchor) // period
        start = anchor + period * index
        return CycleWindow(start=start, end=start + period, index=index)

    months = _MONTH_CYCLES.get(frequency, _FALLBACK_MONTHS)
    elapsed = _whole_months_between(anchor, instant)
    index = elapsed // months
    start = add_months(anchor, index * months)
    return CycleWindow(start=start, end=add_months(anchor, (index + 1) * months), index=index)


def add_months(anchor: dt.datetime, months: int) -> dt.datetime:
    """Shift by whole calendar months, clamping the day to the target month.

    Always applied to the original anchor rather than iteratively, so 31 January
    plus one month plus one month is 31 March and not 28 March. Iterating would
    let a mandate's due date walk backwards a day or two every leap year.
    """
    total = anchor.year * 12 + (anchor.month - 1) + months
    year, zero_based_month = divmod(total, 12)
    month = zero_based_month + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return anchor.replace(year=year, month=month, day=day)


def _whole_months_between(anchor: dt.datetime, at: dt.datetime) -> int:
    """Complete calendar months from ``anchor`` to ``at``; negative before it."""
    estimate = (at.year - anchor.year) * 12 + (at.month - anchor.month)
    if add_months(anchor, estimate) > at:
        estimate -= 1
    return estimate


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        raise ValueError("Anvil refuses naive datetimes; pass a timezone-aware instant")
    return value.astimezone(dt.UTC)
