"""When to retry, and whether to retry at all.

This module is the argument that Anvil uses a language model only where a
language model is genuinely better. Retry timing is a well-posed optimisation
over a tabulated hazard function with abundant labelled data. Asking a model
"when should I retry this?" would be slower, less accurate and -- fatally for a
submission that claims reproducibility -- non-deterministic. So no model is
involved here. There is not even an escalation path to one.

**The decision is not "is now a good time".** It is "given that I hold a finite
number of retry attempts against this mandate, and that each attempt I spend is
one I cannot spend later, when should I spend the next one?" That is a
sequential decision problem, and treating it as a greedy per-attempt choice is
how dunning systems burn three attempts in the 48 hours after a failure and have
nothing left for payday.

So it is solved as one. Let ``A`` be the amount at risk, ``p(k, t)`` the chance
that the *k*-th remaining attempt settles if made at hour ``t``, and ``V(k, t)``
the expected value of playing ``k`` attempts optimally from ``t`` onward::

    V(0, t) = 0
    V(k, t) = max over t' >= t of [ p(k, t')*A + (1 - p(k, t'))*V(k-1, t' + gap) ]

The naive evaluation is quadratic in the horizon. It does not need to be: the
expression inside the max does not depend on ``t``, so ``V(k, ·)`` is a **suffix
maximum** of a function computed once per hour. That makes the whole solve
``O(attempts x horizon_hours)`` -- a few thousand exact Decimal operations, fast
enough to run inline on every case and simple enough to check by hand.

The argmax at each level is the schedule. The value at the top is what the
remaining attempts are worth, which is also the number the planner needs when it
is deciding whether a concession is cheaper than continuing to retry.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from anvil.core.clock import IST, ist_day_of_month, ist_hour
from anvil.domain.enums import FailureClass, RetryPosture
from anvil.domain.money import Money
from anvil.domain.taxonomy import RETRY_CURVES, RetryCurve

#: Minimum hours between two attempts on the same mandate. Issuers treat rapid
#: repeat presentments as abusive, and NPCI's own guidance discourages them, so
#: this is a floor the optimiser is not allowed to undercut however tempting the
#: hazard curve looks.
MIN_GAP_HOURS = 6

#: How far ahead the optimiser looks. Thirty days covers a full salary cycle,
#: which is the longest genuinely useful wait for the balance-driven classes.
DEFAULT_HORIZON_HOURS = 30 * 24

#: Probabilities are carried as integer basis points everywhere they cross a
#: module boundary, so nothing downstream has to decide how to round.
BPS = 10_000


@dataclass(frozen=True, slots=True)
class RankedHour:
    """A candidate hour reduced to what the console's curve chart needs."""

    at: dt.datetime
    probability_bps: int
    value_minor: int
    ist_hour: int
    ist_day: int


@dataclass(frozen=True, slots=True)
class ScheduleDecision:
    """The scheduler's answer, with its reasoning attached.

    ``explanation`` is not decoration. An operator looking at a case that will
    not be retried for eleven days needs to know that this is a considered
    choice about salary cycles rather than a stuck queue, and the planner needs
    ``remaining_value`` to weigh "keep retrying" against "offer a concession".
    """

    should_retry: bool
    failure_class: FailureClass
    posture: RetryPosture
    attempt_number: int
    attempts_remaining: int
    at: dt.datetime | None = None
    probability_bps: int = 0
    #: Expected recovery from playing every remaining attempt optimally.
    remaining_value: Money | None = None
    explanation: str = ""
    refusal_reason: str | None = None
    #: Best hours in rank order, for the curve the console draws.
    ranked: tuple[RankedHour, ...] = ()

    @property
    def probability_percent(self) -> Decimal:
        return (Decimal(self.probability_bps) / Decimal(100)).quantize(Decimal("0.01"))


def _hazard_bps(
    curve: RetryCurve, *, attempt: int, hours_since_failure: int, at: dt.datetime
) -> int:
    """The curve evaluated at one instant, in basis points."""
    probability = curve.probability(
        attempt=attempt,
        hours_since_failure=hours_since_failure,
        hour_of_day=ist_hour(at),
        day_of_month=ist_day_of_month(at),
    )
    return int((probability * BPS).to_integral_value())


def _refuse(
    curve: RetryCurve, attempt_number: int, attempts_remaining: int, reason: str
) -> ScheduleDecision:
    return ScheduleDecision(
        should_retry=False,
        failure_class=curve.failure_class,
        posture=curve.posture,
        attempt_number=attempt_number,
        attempts_remaining=attempts_remaining,
        refusal_reason=reason,
        explanation=reason,
        remaining_value=Money.zero(),
    )


def schedule_next_attempt(
    *,
    failure_class: FailureClass,
    amount_at_risk: Money,
    failed_at: dt.datetime,
    now: dt.datetime,
    attempts_used: int = 0,
    mandate_attempts_remaining: int | None = None,
    mandate_valid_until: dt.datetime | None = None,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
    min_gap_hours: int = MIN_GAP_HOURS,
) -> ScheduleDecision:
    """Choose when to make the next debit attempt, or refuse to make one.

    Every refusal path is explicit and returns a reason, because "no attempt was
    scheduled" is otherwise indistinguishable from "the scheduler never ran",
    and those are very different bugs.
    """
    curve = RETRY_CURVES[failure_class]
    attempt_number = attempts_used + 1

    curve_remaining = max(0, curve.max_attempts - attempts_used)
    attempts_remaining = (
        curve_remaining
        if mandate_attempts_remaining is None
        else min(curve_remaining, mandate_attempts_remaining)
    )

    if curve.posture is RetryPosture.NEVER:
        return _refuse(
            curve,
            attempt_number,
            0,
            f"{failure_class.value} is never worth retrying. {curve.rationale}",
        )
    if curve_remaining <= 0:
        return _refuse(
            curve,
            attempt_number,
            0,
            f"the retry budget for {failure_class.value} is exhausted after "
            f"{attempts_used} attempts; continuing would spend issuer goodwill for nothing",
        )
    if mandate_attempts_remaining is not None and mandate_attempts_remaining <= 0:
        return _refuse(
            curve,
            attempt_number,
            0,
            "the mandate has no debit attempts left in this billing cycle",
        )

    earliest = max(now, failed_at + dt.timedelta(hours=min_gap_hours))
    latest = earliest + dt.timedelta(hours=horizon_hours)
    if mandate_valid_until is not None:
        latest = min(latest, mandate_valid_until)
    if latest < earliest:
        return _refuse(
            curve,
            attempt_number,
            attempts_remaining,
            "the mandate expires before the earliest permitted retry, so there is no "
            "hour left in which an attempt could legitimately be made",
        )

    # Hour buckets, aligned to the top of the hour so results are stable.
    start = earliest.replace(minute=0, second=0, microsecond=0)
    if start < earliest:
        start = start + dt.timedelta(hours=1)
    span = int((latest - start).total_seconds() // 3600) + 1
    if span <= 0:
        return _refuse(
            curve,
            attempt_number,
            attempts_remaining,
            "no whole hour remains between the earliest permitted retry and the mandate expiry",
        )

    hours = [start + dt.timedelta(hours=i) for i in range(span)]
    since = [max(0, int((h - failed_at).total_seconds() // 3600)) for h in hours]
    amount = Decimal(amount_at_risk.minor)

    # --- the dynamic program ------------------------------------------------
    # value[i] is V(k, hours[i]) for the level currently being built; because
    # V(k, t) is a suffix maximum over the same expression, one backward pass
    # per level is enough. best[i] records the argmax so the schedule can be
    # read back off the bottom level.
    continuation = [Decimal(0)] * (span + 1)
    chosen_index = 0
    chosen_probability = 0
    level_values: list[Decimal] = []
    level_probabilities: list[int] = []

    for level in range(1, attempts_remaining + 1):
        attempt_at_level = attempts_used + (attempts_remaining - level) + 1
        immediate: list[Decimal] = []
        probabilities: list[int] = []
        for i, hour in enumerate(hours):
            p_bps = _hazard_bps(
                curve, attempt=attempt_at_level, hours_since_failure=since[i], at=hour
            )
            probabilities.append(p_bps)
            p = Decimal(p_bps) / Decimal(BPS)
            follow_index = min(span, i + min_gap_hours)
            immediate.append(p * amount + (Decimal(1) - p) * continuation[follow_index])

        # Backward suffix max, carrying the argmax with it.
        suffix = [Decimal(0)] * (span + 1)
        argmax = [0] * (span + 1)
        for i in range(span - 1, -1, -1):
            if immediate[i] >= suffix[i + 1]:
                suffix[i] = immediate[i]
                argmax[i] = i
            else:
                suffix[i] = suffix[i + 1]
                argmax[i] = argmax[i + 1]

        continuation = suffix
        level_values = immediate
        level_probabilities = probabilities
        chosen_index = argmax[0]
        chosen_probability = probabilities[chosen_index]

    chosen_at = hours[chosen_index]
    remaining_value = Money(int(continuation[0].to_integral_value()), amount_at_risk.currency)

    ranked = tuple(
        sorted(
            (
                RankedHour(
                    at=hours[i],
                    probability_bps=level_probabilities[i],
                    value_minor=int(level_values[i].to_integral_value()),
                    ist_hour=ist_hour(hours[i]),
                    ist_day=ist_day_of_month(hours[i]),
                )
                for i in range(span)
            ),
            key=lambda r: (-r.value_minor, r.at),
        )[:24]
    )

    return ScheduleDecision(
        should_retry=True,
        failure_class=failure_class,
        posture=curve.posture,
        attempt_number=attempt_number,
        attempts_remaining=attempts_remaining,
        at=chosen_at,
        probability_bps=chosen_probability,
        remaining_value=remaining_value,
        explanation=explain(curve, chosen_at, chosen_probability, attempt_number, now),
        ranked=ranked,
    )


def explain(
    curve: RetryCurve,
    at: dt.datetime,
    probability_bps: int,
    attempt_number: int,
    now: dt.datetime,
) -> str:
    """Say, in a sentence an operator can check, why this hour won.

    The reasons cited are the actual factors in the hazard function, not a
    post-hoc story: if the salary-cycle multiplier is what moved the number,
    that is what the sentence says.
    """
    local = at.astimezone(IST)
    wait_hours = max(0, int((at - now).total_seconds() // 3600))
    parts: list[str] = []

    if curve.salary_sensitive:
        if local.day in (1, 2, 3) or local.day >= 28:
            parts.append(
                f"the {_ordinal(local.day)} sits on the salary-credit peak, when balances recover"
            )
        elif 15 <= local.day <= 24:
            parts.append(
                f"the {_ordinal(local.day)} is mid-cycle and thin, but it is the best hour "
                "available inside the mandate's validity window"
            )
    if curve.circadian_sensitive:
        if 9 <= local.hour <= 18:
            parts.append(
                f"{local.hour:02d}:00 IST is clear of the overnight issuer maintenance window"
            )
        else:
            parts.append(f"{local.hour:02d}:00 IST is the best remaining slot")

    when = "now" if wait_hours == 0 else f"in {_humanise_hours(wait_hours)}"
    reason = "; ".join(parts) if parts else "it maximises expected recovery across the horizon"
    return (
        f"Attempt {attempt_number} scheduled {when}, at "
        f"{local.strftime('%a %d %b %H:%M')} IST, at {probability_bps / 100:.1f}% expected "
        f"success, because {reason}."
    )


def _ordinal(day: int) -> str:
    if 11 <= day <= 13:
        return f"{day}th"
    return f"{day}{('th', 'st', 'nd', 'rd')[day % 10] if day % 10 < 4 else 'th'}"


def _humanise_hours(hours: int) -> str:
    if hours < 24:
        return f"{hours}h"
    days, rest = divmod(hours, 24)
    return f"{days}d" if rest == 0 else f"{days}d {rest}h"


def value_of_retrying(
    *,
    failure_class: FailureClass,
    amount_at_risk: Money,
    failed_at: dt.datetime,
    now: dt.datetime,
    attempts_used: int = 0,
    mandate_attempts_remaining: int | None = None,
    mandate_valid_until: dt.datetime | None = None,
) -> Money:
    """What the remaining retry attempts are worth, in expectation.

    The planner compares this against the cost of a concession. Offering ₹200 to
    save a subscription whose remaining retries are already worth ₹1,100 in
    expectation is giving money away; offering it when they are worth ₹40 is
    good business. Having the number makes that an arithmetic question rather
    than a matter of taste.
    """
    decision = schedule_next_attempt(
        failure_class=failure_class,
        amount_at_risk=amount_at_risk,
        failed_at=failed_at,
        now=now,
        attempts_used=attempts_used,
        mandate_attempts_remaining=mandate_attempts_remaining,
        mandate_valid_until=mandate_valid_until,
    )
    return decision.remaining_value or Money.zero(amount_at_risk.currency)
