"""Finding revenue that is slipping away, including before it has slipped.

The track asks for agents that "detect revenue at risk". The obvious reading is
"find the failed debits", and Anvil does that. But a subscription whose debits
are succeeding *only after two retries* is also at risk, and it is at risk
earlier, when there is still time to fix the instrument before a cycle is missed.
Both are detected here, and they are reported as different things because they
deserve different playbooks.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from anvil.domain.enums import FailureClass
from anvil.domain.money import Money


class RiskSignal(StrEnum):
    """Why a subscription was flagged. Drives which playbook opens."""

    DEBIT_FAILED = "debit_failed"
    MANDATE_EXPIRING = "mandate_expiring"
    INSTRUMENT_EXPIRING = "instrument_expiring"
    DEGRADING = "degrading"
    ATTEMPTS_NEARLY_EXHAUSTED = "attempts_nearly_exhausted"
    REPEATED_LATE_SETTLEMENT = "repeated_late_settlement"


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One historical debit attempt, as detection sees it."""

    at: dt.datetime
    succeeded: bool
    attempt_number: int
    failure_class: FailureClass | None = None


@dataclass(frozen=True, slots=True)
class SubscriptionSnapshot:
    """Everything detection needs about one subscription. No I/O."""

    subscription_id: str
    customer_id: str
    amount: Money
    current_period_end: dt.datetime
    consecutive_failures: int = 0
    recent_attempts: tuple[AttemptRecord, ...] = ()
    mandate_valid_until: dt.datetime | None = None
    mandate_attempts_remaining: int | None = None
    instrument_expires_at: dt.datetime | None = None
    has_open_case: bool = False


@dataclass(frozen=True, slots=True)
class Detection:
    """One reason one subscription is at risk."""

    subscription_id: str
    customer_id: str
    signal: RiskSignal
    amount_at_risk: Money
    detected_at: dt.datetime
    detail: str
    failure_class: FailureClass | None = None
    failed_at: dt.datetime | None = None
    #: Higher is more urgent. Used only to order the detector's own output; the
    #: authoritative ranking is anvil.risk.scoring.priority.
    urgency: int = 0


#: How far ahead an expiry counts as "coming". Chosen to give a full billing
#: cycle plus a fortnight of nudging, which is roughly what it takes to get a
#: customer to update a card without it becoming an emergency.
EXPIRY_LOOKAHEAD_DAYS = 45

#: A subscription that needed this many attempts on average across its recent
#: history is degrading even if every cycle eventually settled.
DEGRADATION_ATTEMPT_THRESHOLD = 2


def _class_label(failure_class: FailureClass | None) -> str:
    return failure_class.value if failure_class is not None else "unclassified"


def detect(snapshot: SubscriptionSnapshot, *, now: dt.datetime) -> list[Detection]:
    """Every risk signal this subscription currently exhibits.

    Returns a list rather than a single verdict because a subscription can be
    at risk for more than one reason at once -- a failed debit against a mandate
    that also expires next week is a materially more urgent case than either
    alone, and collapsing that into one signal would lose it.
    """
    found: list[Detection] = []

    last_failure = next(
        (
            a
            for a in sorted(snapshot.recent_attempts, key=lambda a: a.at, reverse=True)
            if not a.succeeded
        ),
        None,
    )
    if snapshot.consecutive_failures > 0:
        detail = f"{snapshot.consecutive_failures} consecutive failed debit(s)"
        if last_failure is not None:
            detail += f"; most recent {_class_label(last_failure.failure_class)}"
        else:
            detail += (
                "; no attempt record inside the retention window, so the failure class "
                "must be re-established before a retry is scheduled"
            )
        found.append(
            Detection(
                subscription_id=snapshot.subscription_id,
                customer_id=snapshot.customer_id,
                signal=RiskSignal.DEBIT_FAILED,
                amount_at_risk=snapshot.amount,
                detected_at=now,
                failure_class=last_failure.failure_class if last_failure else None,
                failed_at=last_failure.at if last_failure else None,
                urgency=700 + min(150, snapshot.consecutive_failures * 60),
                detail=detail,
            )
        )

    if snapshot.mandate_valid_until is not None:
        days = (snapshot.mandate_valid_until - now).days
        if 0 <= days <= EXPIRY_LOOKAHEAD_DAYS:
            found.append(
                Detection(
                    subscription_id=snapshot.subscription_id,
                    customer_id=snapshot.customer_id,
                    signal=RiskSignal.MANDATE_EXPIRING,
                    amount_at_risk=snapshot.amount,
                    detected_at=now,
                    urgency=500 + max(0, (EXPIRY_LOOKAHEAD_DAYS - days) * 4),
                    detail=(
                        f"the mandate expires in {days} day(s); after that there is no "
                        "authorisation to debit against and recovery becomes a "
                        "re-authorisation journey"
                    ),
                )
            )

    if snapshot.instrument_expires_at is not None:
        days = (snapshot.instrument_expires_at - now).days
        if 0 <= days <= EXPIRY_LOOKAHEAD_DAYS:
            found.append(
                Detection(
                    subscription_id=snapshot.subscription_id,
                    customer_id=snapshot.customer_id,
                    signal=RiskSignal.INSTRUMENT_EXPIRING,
                    amount_at_risk=snapshot.amount,
                    detected_at=now,
                    urgency=450 + max(0, (EXPIRY_LOOKAHEAD_DAYS - days) * 4),
                    detail=(
                        f"the payment instrument expires in {days} day(s). Asking now "
                        "costs one message; asking after it fails costs a missed cycle"
                    ),
                )
            )

    settled = [a for a in snapshot.recent_attempts if a.succeeded]
    if len(settled) >= 3:
        mean_attempts = sum(a.attempt_number for a in settled) / len(settled)
        if mean_attempts >= DEGRADATION_ATTEMPT_THRESHOLD:
            found.append(
                Detection(
                    subscription_id=snapshot.subscription_id,
                    customer_id=snapshot.customer_id,
                    signal=RiskSignal.DEGRADING,
                    amount_at_risk=snapshot.amount,
                    detected_at=now,
                    urgency=300,
                    detail=(
                        f"the last {len(settled)} cycles settled, but only after "
                        f"{mean_attempts:.1f} attempts on average. This subscription is "
                        "failing slowly and will miss a cycle before long"
                    ),
                )
            )

    if (
        snapshot.mandate_attempts_remaining is not None
        and snapshot.mandate_attempts_remaining <= 1
        and snapshot.consecutive_failures > 0
    ):
        found.append(
            Detection(
                subscription_id=snapshot.subscription_id,
                customer_id=snapshot.customer_id,
                signal=RiskSignal.ATTEMPTS_NEARLY_EXHAUSTED,
                amount_at_risk=snapshot.amount,
                detected_at=now,
                urgency=900,
                detail=(
                    f"only {snapshot.mandate_attempts_remaining} debit attempt(s) remain "
                    "in this cycle. The next attempt is the last one, so its timing "
                    "carries the whole cycle"
                ),
            )
        )

    return sorted(found, key=lambda d: (-d.urgency, d.signal.value))


def detect_all(
    snapshots: Sequence[SubscriptionSnapshot],
    *,
    now: dt.datetime,
    skip_with_open_case: bool = True,
) -> list[Detection]:
    """Sweep a book. Ordered most urgent first.

    Subscriptions already being worked are skipped by default: opening a second
    case for the same subscription would double-count the money at risk and
    would let two graphs contact the same customer independently, which is
    exactly the frequency-cap failure the policy engine exists to prevent.
    """
    out: list[Detection] = []
    for snapshot in snapshots:
        if skip_with_open_case and snapshot.has_open_case:
            continue
        out.extend(detect(snapshot, now=now))
    return sorted(out, key=lambda d: (-d.urgency, d.subscription_id, d.signal.value))


def total_at_risk(detections: Sequence[Detection]) -> Money:
    """Sum the money at risk, counting each subscription only once.

    A subscription flagged by three signals is not three times the money, and a
    dashboard that says otherwise is lying about the size of the problem.
    """
    seen: dict[str, Money] = {}
    for d in detections:
        seen.setdefault(d.subscription_id, d.amount_at_risk)
    total = Money.zero()
    for amount in seen.values():
        total = total + amount
    return total
