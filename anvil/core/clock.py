"""An injectable clock.

Nothing in Anvil calls ``datetime.now()`` directly. Every time-dependent
decision -- retry scheduling, quiet hours, mandate validity, frequency caps --
goes through a :class:`Clock`, so tests can place the system at any instant and
the simulator can run a month of recovery activity in a second of wall time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Protocol

#: Indian Standard Time. Quiet hours, salary cycles and issuer maintenance
#: windows are all IST concepts, so they are computed in IST and stored in UTC.
IST = timezone(timedelta(hours=5, minutes=30), name="IST")


class Clock(Protocol):
    def now(self) -> datetime:
        """Current instant, always timezone-aware UTC."""
        ...


class SystemClock:
    """Wall-clock time. The only implementation used in production paths."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """A clock the caller drives. Used by tests and the simulator."""

    __slots__ = ("_now",)

    def __init__(self, at: datetime) -> None:
        self._now = _require_aware(at)

    def now(self) -> datetime:
        return self._now

    def set(self, at: datetime) -> None:
        self._now = _require_aware(at)

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        return self._now

    def advance_hours(self, hours: float) -> datetime:
        return self.advance(timedelta(hours=hours))

    def advance_days(self, days: float) -> datetime:
        return self.advance(timedelta(days=days))


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Anvil refuses naive datetimes; pass a timezone-aware value")
    return value.astimezone(UTC)


def to_ist(value: datetime) -> datetime:
    return _require_aware(value).astimezone(IST)


def ist_hour(value: datetime) -> int:
    return to_ist(value).hour


def ist_day_of_month(value: datetime) -> int:
    return to_ist(value).day


def hours_between(earlier: datetime, later: datetime) -> int:
    """Whole hours from ``earlier`` to ``later``, floored at zero."""
    delta = _require_aware(later) - _require_aware(earlier)
    return max(0, int(delta.total_seconds() // 3600))
