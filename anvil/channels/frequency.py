"""Contact-frequency caps, minimum gaps and IST quiet hours.

These are stopping rules, and ``docs/explanation/architecture.md`` section 3 is explicit
that a stopping rule a model can talk itself out of is not a stopping rule. So
everything here is arithmetic over the append-only
:class:`~anvil.db.models.comms.ContactLedger`, evaluated by a pure function with
no model, no I/O and no discretion.

The distinction the module exists to get right
----------------------------------------------

A dunning email and a step-up authentication challenge are both "outreach" to a
naive frequency limiter, and treating them identically breaks the product in one
direction or the law in the other. Anvil separates three questions that are
usually collapsed into one:

**Does this message count?** Always. Every contact that goes out is written to
the contact ledger, whatever its purpose. A step-up challenge sent at 14:00 is
real intrusion on the customer's attention and must make the 15:00 dunning
message wait. Exemptions below are exemptions from being *blocked*, never from
being *counted* -- that asymmetry is the whole trick.

**Is it capped?** Promotional caps apply to promotional purposes only.
``PROMOTIONAL_WINBACK`` is the single promotional purpose in the vocabulary; the
rest are service messages about a payment the customer already agreed to make.
Suppressing a step-up challenge under a marketing cap would silently break an
authentication the customer is standing in front of, so transactional purposes
are never tested against the promotional caps. They are still tested against the
overall caps, because "we sent you eleven service messages today" is a bad
outcome regardless of what each one was about. ``STEP_UP_AUTHENTICATION`` is the
one purpose exempt from the overall caps as well: it is the second half of an
action the customer themselves initiated, and there is no useful sense in which
we can decline to finish it.

**Is now an acceptable hour?** Quiet hours apply to everything, including
step-up. The single exemption is a message that is both step-up *and* flagged
``time_critical`` by its caller -- meaning a person is at that moment waiting on
it. A step-up challenge at 23:05 because the customer just tapped "pay now" is
not an intrusion; a step-up challenge at 23:05 because the agent decided to
start a recovery is. Only the caller knows which one it has, so only the caller
can set the flag, and it is refused outright on promotional messages.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from anvil.channels.base import OutboundMessage
from anvil.core.clock import IST, to_ist
from anvil.core.errors import ValidationError
from anvil.db.models.comms import ContactLedger
from anvil.domain.enums import DeliveryStatus, MessagePurpose

__all__ = [
    "CAP_EXEMPT_PURPOSES",
    "DEFAULT_FREQUENCY_POLICY",
    "QUIET_HOURS_EXEMPT_PURPOSES",
    "ContactRepository",
    "FrequencyDecision",
    "FrequencyPolicy",
    "evaluate_frequency",
    "in_quiet_hours",
    "next_quiet_hours_end",
]


#: Purposes that may not be blocked by the overall contact caps. They still
#: write to the contact ledger and so still push other messages out.
CAP_EXEMPT_PURPOSES: frozenset[MessagePurpose] = frozenset({MessagePurpose.STEP_UP_AUTHENTICATION})

#: Purposes eligible for a quiet-hours exemption. Eligibility is necessary and
#: not sufficient -- the caller must also assert ``time_critical``.
QUIET_HOURS_EXEMPT_PURPOSES: frozenset[MessagePurpose] = frozenset(
    {MessagePurpose.STEP_UP_AUTHENTICATION}
)


@dataclass(frozen=True, slots=True)
class FrequencyPolicy:
    """The caps, in one immutable object.

    Defaults are deliberately conservative. A merchant may loosen them through
    its compiled policy bundle, but the shipped numbers are what a reasonable
    person would accept receiving: at most two service messages a day, at most
    five a week, four hours apart, and at most one marketing message a day.
    """

    max_contacts_24h: int = 2
    max_contacts_7d: int = 5
    min_gap_minutes: int = 240
    max_promotional_24h: int = 1
    max_promotional_7d: int = 2
    promotional_min_gap_minutes: int = 1440
    #: IST local hours. ``start`` is inclusive, ``end`` is exclusive, and the
    #: window is allowed to wrap midnight -- 21 to 8 is the shipped default and
    #: matches ``merchants.quiet_hours_start`` / ``quiet_hours_end``.
    quiet_hours_start_ist: int = 21
    quiet_hours_end_ist: int = 8

    def __post_init__(self) -> None:
        for name in (
            "max_contacts_24h",
            "max_contacts_7d",
            "max_promotional_24h",
            "max_promotional_7d",
        ):
            if getattr(self, name) < 0:
                raise ValidationError(f"FrequencyPolicy.{name} must not be negative")
        for name in ("min_gap_minutes", "promotional_min_gap_minutes"):
            if getattr(self, name) < 0:
                raise ValidationError(f"FrequencyPolicy.{name} must not be negative")
        for name in ("quiet_hours_start_ist", "quiet_hours_end_ist"):
            if not 0 <= getattr(self, name) <= 23:
                raise ValidationError(f"FrequencyPolicy.{name} must be an IST hour 0-23")

    @property
    def has_quiet_hours(self) -> bool:
        """Equal bounds mean no quiet window at all, not a 24-hour one.

        A merchant that wants to send at any hour sets both to the same value.
        Reading that as "silence all day" would take a permissive configuration
        and make it maximally restrictive, which is the wrong way to be wrong
        about a merchant's intent.
        """
        return self.quiet_hours_start_ist != self.quiet_hours_end_ist

    @classmethod
    def for_merchant(
        cls, *, quiet_hours_start: int, quiet_hours_end: int, **overrides: int
    ) -> FrequencyPolicy:
        """Build from the merchant row's quiet-hour columns plus any cap overrides."""
        return cls(
            quiet_hours_start_ist=quiet_hours_start,
            quiet_hours_end_ist=quiet_hours_end,
            **overrides,
        )


DEFAULT_FREQUENCY_POLICY = FrequencyPolicy()


@dataclass(frozen=True, slots=True)
class _Violation:
    """One broken constraint, with the instant it stops being broken."""

    kind: str
    status: DeliveryStatus
    reason: str
    clears_at: dt.datetime
    #: Lower sorts first when two violations clear at the same instant.
    precedence: int


@dataclass(frozen=True, slots=True)
class FrequencyDecision:
    """Whether this message may go out now, and if not, when it may.

    ``earliest_allowed_at`` accounts for *every* constraint, not only the one
    named in ``reason``. A caller that reschedules to the reported reason's
    clearing time alone would come straight back into a different suppression,
    so the decision does that arithmetic once, here.
    """

    allowed: bool
    status: DeliveryStatus | None
    reason: str
    contacts_24h: int
    contacts_7d: int
    promotional_24h: int
    promotional_7d: int
    minutes_since_last_contact: int | None
    earliest_allowed_at: dt.datetime | None
    exemptions: tuple[str, ...] = field(default=())

    @property
    def suppressed_by_quiet_hours(self) -> bool:
        return self.status is DeliveryStatus.SUPPRESSED_QUIET_HOURS

    @property
    def suppressed_by_cap(self) -> bool:
        return self.status is DeliveryStatus.SUPPRESSED_FREQUENCY_CAP


class ContactRepository(Protocol):
    """Read and append side of the contact ledger.

    The read returns rows rather than counts because the evaluator needs the
    oldest contact in each window to say *when* the cap clears, and because a
    count computed in SQL is a count nobody can unit-test.
    """

    async def recent_contacts(
        self, customer_id: str, since: dt.datetime
    ) -> Sequence[ContactLedger]:
        """Every contact for this customer at or after ``since``, any order."""
        ...

    async def add_contact(self, contact: ContactLedger) -> None:
        """Stage one contact row in the caller's transaction."""
        ...


def in_quiet_hours(when: dt.datetime, policy: FrequencyPolicy) -> bool:
    """Is ``when`` inside the merchant's IST quiet window?

    Computed in IST because quiet hours are a statement about when a person is
    asleep, not about UTC. The window may wrap midnight, which the naive
    ``start <= hour < end`` comparison gets wrong for the common 21-to-8 case.
    """
    if not policy.has_quiet_hours:
        return False
    hour = to_ist(when).hour
    start, end = policy.quiet_hours_start_ist, policy.quiet_hours_end_ist
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def next_quiet_hours_end(when: dt.datetime, policy: FrequencyPolicy) -> dt.datetime:
    """The next instant at which the quiet window is over, in UTC.

    IST is a fixed offset with no daylight saving, so replacing the hour on the
    local wall clock is exact -- there is no ambiguous or skipped local time to
    reason about.
    """
    local = to_ist(when)
    boundary = local.replace(hour=policy.quiet_hours_end_ist, minute=0, second=0, microsecond=0)
    if boundary <= local:
        boundary = boundary + dt.timedelta(days=1)
    return boundary.astimezone(dt.UTC)


def evaluate_frequency(
    *,
    message: OutboundMessage,
    contacts: Sequence[ContactLedger],
    policy: FrequencyPolicy = DEFAULT_FREQUENCY_POLICY,
    now: dt.datetime,
) -> FrequencyDecision:
    """Decide whether ``message`` may be sent at ``now``. Pure and total.

    ``contacts`` must cover at least the last seven days for this customer;
    anything older is ignored, and anything in the future is treated as a
    contact that has already happened, which is the conservative reading.

    When several constraints are broken at once the one reported is the one that
    clears last, because that is the one the caller actually has to wait for.
    """
    if now.tzinfo is None:
        raise ValidationError("frequency must be evaluated at a timezone-aware instant")
    if message.customer_id and any(c.customer_id != message.customer_id for c in contacts):
        raise ValidationError(
            "contact ledger rows for a different customer were passed to the evaluator",
            customer_id=message.customer_id,
        )

    day_start = now - dt.timedelta(hours=24)
    week_start = now - dt.timedelta(days=7)

    in_day = [c for c in contacts if c.contacted_at > day_start]
    in_week = [c for c in contacts if c.contacted_at > week_start]
    promo_day = [c for c in in_day if not c.purpose.is_transactional]
    promo_week = [c for c in in_week if not c.purpose.is_transactional]

    last_contact = max((c.contacted_at for c in contacts), default=None)
    last_promo = max(
        (c.contacted_at for c in contacts if not c.purpose.is_transactional), default=None
    )
    minutes_since_last = (
        int((now - last_contact).total_seconds() // 60) if last_contact is not None else None
    )

    exemptions: list[str] = []
    violations: list[_Violation] = []

    cap_exempt = message.purpose in CAP_EXEMPT_PURPOSES
    if cap_exempt:
        exemptions.append("overall caps waived: step-up completes an action the customer initiated")

    if message.is_promotional:
        _check_count(
            violations,
            count=len(promo_day),
            limit=policy.max_promotional_24h,
            window=promo_day,
            window_length=dt.timedelta(hours=24),
            now=now,
            kind="promotional_24h",
            label="promotional messages in 24h",
            precedence=10,
        )
        _check_count(
            violations,
            count=len(promo_week),
            limit=policy.max_promotional_7d,
            window=promo_week,
            window_length=dt.timedelta(days=7),
            now=now,
            kind="promotional_7d",
            label="promotional messages in 7d",
            precedence=11,
        )
        _check_gap(
            violations,
            last=last_promo,
            gap_minutes=policy.promotional_min_gap_minutes,
            now=now,
            kind="promotional_min_gap",
            label="promotional minimum gap",
            precedence=12,
        )

    if not cap_exempt:
        _check_count(
            violations,
            count=len(in_day),
            limit=policy.max_contacts_24h,
            window=in_day,
            window_length=dt.timedelta(hours=24),
            now=now,
            kind="contacts_24h",
            label="contacts in 24h",
            precedence=20,
        )
        _check_count(
            violations,
            count=len(in_week),
            limit=policy.max_contacts_7d,
            window=in_week,
            window_length=dt.timedelta(days=7),
            now=now,
            kind="contacts_7d",
            label="contacts in 7d",
            precedence=21,
        )
        _check_gap(
            violations,
            last=last_contact,
            gap_minutes=policy.min_gap_minutes,
            now=now,
            kind="min_gap",
            label="minimum gap between contacts",
            precedence=22,
        )

    quiet_exempt = message.purpose in QUIET_HOURS_EXEMPT_PURPOSES and message.time_critical
    if quiet_exempt:
        exemptions.append("quiet hours waived: time-critical step-up, a person is waiting on it")
    elif in_quiet_hours(now, policy):
        clears = next_quiet_hours_end(now, policy)
        violations.append(
            _Violation(
                kind="quiet_hours",
                status=DeliveryStatus.SUPPRESSED_QUIET_HOURS,
                reason=(
                    f"inside IST quiet hours "
                    f"{policy.quiet_hours_start_ist:02d}:00-{policy.quiet_hours_end_ist:02d}:00; "
                    f"local time was {to_ist(now).strftime('%H:%M')} IST"
                ),
                clears_at=clears,
                precedence=30,
            )
        )

    counts = {
        "contacts_24h": len(in_day),
        "contacts_7d": len(in_week),
        "promotional_24h": len(promo_day),
        "promotional_7d": len(promo_week),
    }

    if not violations:
        return FrequencyDecision(
            allowed=True,
            status=None,
            reason="within all contact limits",
            minutes_since_last_contact=minutes_since_last,
            earliest_allowed_at=now,
            exemptions=tuple(exemptions),
            **counts,
        )

    binding = max(violations, key=lambda v: (v.clears_at, -v.precedence))
    earliest = max(v.clears_at for v in violations)
    return FrequencyDecision(
        allowed=False,
        status=binding.status,
        reason=binding.reason,
        minutes_since_last_contact=minutes_since_last,
        earliest_allowed_at=earliest,
        exemptions=tuple(exemptions),
        **counts,
    )


def _check_count(
    violations: list[_Violation],
    *,
    count: int,
    limit: int,
    window: Sequence[ContactLedger],
    window_length: dt.timedelta,
    now: dt.datetime,
    kind: str,
    label: str,
    precedence: int,
) -> None:
    """Record a violation when a rolling-window count is already at its limit.

    The window clears when the oldest contact in it rolls out, which is what
    makes ``earliest_allowed_at`` a real answer rather than a guess. With an
    empty window and a zero limit there is nothing to roll out, so the cap is
    treated as permanent for the length of the window.
    """
    if count < limit:
        return
    oldest = min((c.contacted_at for c in window), default=None)
    clears_at = (oldest + window_length) if oldest is not None else (now + window_length)
    violations.append(
        _Violation(
            kind=kind,
            status=DeliveryStatus.SUPPRESSED_FREQUENCY_CAP,
            reason=f"{label} is at its cap of {limit} (currently {count})",
            clears_at=clears_at,
            precedence=precedence,
        )
    )


def _check_gap(
    violations: list[_Violation],
    *,
    last: dt.datetime | None,
    gap_minutes: int,
    now: dt.datetime,
    kind: str,
    label: str,
    precedence: int,
) -> None:
    if last is None or gap_minutes <= 0:
        return
    clears_at = last + dt.timedelta(minutes=gap_minutes)
    if clears_at <= now:
        return
    elapsed = int((now - last).total_seconds() // 60)
    violations.append(
        _Violation(
            kind=kind,
            status=DeliveryStatus.SUPPRESSED_FREQUENCY_CAP,
            reason=f"{label} is {gap_minutes} minutes; only {elapsed} have passed",
            clears_at=clears_at,
            precedence=precedence,
        )
    )


def describe_quiet_window(policy: FrequencyPolicy) -> str:
    """Human-readable quiet window, for the console and suppression reasons."""
    if not policy.has_quiet_hours:
        return "no quiet hours configured"
    return (
        f"{policy.quiet_hours_start_ist:02d}:00-{policy.quiet_hours_end_ist:02d}:00 "
        f"{IST.tzname(None) or 'IST'}"
    )
