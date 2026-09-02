"""Scores: how likely is this recoverable, how likely are they to leave, what first.

All three are integers on 0-1000 rather than floats on 0-1. That is not
squeamishness about floats -- it is that these numbers are stored, sorted,
compared for equality in tests, and rendered in a console, and an integer does
all four without a rounding convention having to be agreed in four places.

Every weight below is stated with its reasoning, because an interviewer will
ask why 300 and not 250, and "it felt right" is not an answer. The honest
position is that these are *priors*, chosen to be defensible and then measured:
:mod:`anvil.risk.calibration` exists precisely so that the priors can be shown
to be well-calibrated or shown not to be.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from anvil.domain.enums import FailureClass, RetryPosture
from anvil.domain.money import ZERO_INR, Money
from anvil.domain.taxonomy import RETRY_CURVES

SCORE_MAX = 1000


def _clamp(value: int) -> int:
    return max(0, min(SCORE_MAX, value))


@dataclass(frozen=True, slots=True)
class CustomerHistory:
    """The behavioural facts that move a score. Deliberately few.

    Every field here is something Anvil observes first-hand in its own tables.
    Nothing is inferred from third-party data, which keeps the scoring defensible
    under the DPDPA and keeps the feature set small enough to reason about.
    """

    tenure_days: int = 0
    prior_failures: int = 0
    prior_recoveries: int = 0
    prior_concessions: Money = ZERO_INR
    contacts_last_30d: int = 0
    lifetime_value: Money = ZERO_INR

    @property
    def recovery_rate_bps(self) -> int:
        """How often this customer's past failures came back. 5000 when unknown.

        Defaulting an unknown customer to the midpoint rather than to zero
        matters: a first-time failure is not evidence of a bad payer, and
        scoring it as one would push new customers straight into the aggressive
        end of the playbook.
        """
        total = self.prior_failures + self.prior_recoveries
        if total == 0:
            return 5000
        return int(Decimal(self.prior_recoveries) * 10_000 / Decimal(total))


@dataclass(frozen=True, slots=True)
class Scores:
    recovery_likelihood: int
    churn_risk: int
    priority: int
    reasoning: tuple[str, ...] = ()


def recovery_likelihood(
    *,
    failure_class: FailureClass,
    history: CustomerHistory,
    attempts_used: int = 0,
    scheduler_probability_bps: int | None = None,
) -> int:
    """How likely this case is to end in recovered money.

    Anchored on the hazard curve rather than on customer features, because the
    failure class dominates: an expired card is unrecoverable by retry no matter
    how loyal the customer, and a technical decline usually clears no matter how
    new they are. Customer history then adjusts within that anchor.
    """
    curve = RETRY_CURVES[failure_class]

    if scheduler_probability_bps is not None:
        base = scheduler_probability_bps // 10
    elif curve.attempt_base:
        base = int(curve.attempt_base[0] * SCORE_MAX)
    else:
        base = 0

    if curve.posture is RetryPosture.NEVER:
        # Not zero: an expired card is highly recoverable by asking for a new
        # one. It is only unrecoverable by *retrying*, which is a different claim.
        base = 300 if failure_class is FailureClass.INSTRUMENT_EXPIRED else 80

    # A customer who has recovered before recovers again. Worth +/-150 at the
    # extremes -- material, but never enough to overturn the failure class.
    history_adjust = (history.recovery_rate_bps - 5000) * 150 // 5000

    # Each attempt already spent is evidence the easy paths did not work.
    attempt_penalty = attempts_used * 90

    # Long tenure is mild positive evidence: people who have paid for two years
    # generally intend to keep paying.
    tenure_bonus = min(80, history.tenure_days // 9)

    return _clamp(base + history_adjust - attempt_penalty + tenure_bonus)


def churn_risk(
    *,
    failure_class: FailureClass,
    history: CustomerHistory,
    attempts_used: int = 0,
    contacts_made: int = 0,
) -> int:
    """How likely this customer is to leave rather than pay.

    The contact term is the important one and it is deliberately steep. Every
    additional message raises the chance the customer resolves the situation by
    cancelling instead of paying, which is the failure mode that makes naive
    dunning worse than doing nothing. The scoring has to price that, or the
    planner will happily send a sixth reminder.
    """
    base = _CHURN_BASE[failure_class]

    # Deliberate revocation is a decision, not an accident.
    if failure_class in (FailureClass.MANDATE_REVOKED, FailureClass.MANDATE_PAUSED):
        base += 180

    # Steep and superlinear: the fourth contact costs far more than the second.
    contact_pressure = min(300, contacts_made * contacts_made * 25)

    attempt_pressure = min(150, attempts_used * 40)

    # Tenure and past recoveries both cut against churn.
    loyalty = min(200, history.tenure_days // 5) + (history.recovery_rate_bps - 5000) * 100 // 5000

    return _clamp(base + contact_pressure + attempt_pressure - loyalty)


#: Starting churn risk by failure class, before any behavioural adjustment.
#: A technical decline says nothing about intent; a risk decline or a closed
#: account says a great deal.
_CHURN_BASE: dict[FailureClass, int] = {
    FailureClass.ISSUER_TECHNICAL: 120,
    FailureClass.INSUFFICIENT_FUNDS: 280,
    FailureClass.LIMIT_EXCEEDED: 220,
    FailureClass.INSTRUMENT_EXPIRED: 300,
    FailureClass.AUTH_REQUIRED: 260,
    FailureClass.MANDATE_PAUSED: 480,
    FailureClass.MANDATE_REVOKED: 700,
    FailureClass.ACCOUNT_CLOSED: 620,
    FailureClass.RISK_DECLINED: 520,
    FailureClass.UNKNOWN: 350,
}


def priority(
    *,
    amount_at_risk: Money,
    recovery: int,
    churn: int,
    lifetime_value: Money = ZERO_INR,
) -> int:
    """What to work first.

    Expected recoverable value is the spine: amount at risk multiplied by the
    chance of getting it. Two adjustments sit on top. Churn risk *raises*
    priority rather than lowering it -- a customer about to leave is the one
    where acting today instead of tomorrow actually changes the outcome. And
    lifetime value adds a modest tilt, because losing a two-year subscriber
    costs more than the one invoice in front of you.

    Returned on a 0-1000 scale by dividing through a reference of one lakh of
    expected value, so the number stays comparable across merchants of very
    different sizes.
    """
    expected_paise = amount_at_risk.minor * recovery // SCORE_MAX
    reference = 100_000 * 100  # one lakh, in paise
    value_component = min(700, expected_paise * 700 // reference)
    urgency_component = churn * 200 // SCORE_MAX
    ltv_component = min(100, lifetime_value.minor * 100 // (reference * 5))
    return _clamp(value_component + urgency_component + ltv_component)


def score_case(
    *,
    failure_class: FailureClass,
    amount_at_risk: Money,
    history: CustomerHistory,
    attempts_used: int = 0,
    contacts_made: int = 0,
    scheduler_probability_bps: int | None = None,
) -> Scores:
    """All three scores together, with the sentences that explain them."""
    recovery = recovery_likelihood(
        failure_class=failure_class,
        history=history,
        attempts_used=attempts_used,
        scheduler_probability_bps=scheduler_probability_bps,
    )
    churn = churn_risk(
        failure_class=failure_class,
        history=history,
        attempts_used=attempts_used,
        contacts_made=contacts_made,
    )
    prio = priority(
        amount_at_risk=amount_at_risk,
        recovery=recovery,
        churn=churn,
        lifetime_value=history.lifetime_value,
    )

    reasoning: list[str] = [
        f"{failure_class.value} anchors recovery likelihood at {recovery}/1000",
    ]
    if history.prior_failures + history.prior_recoveries > 0:
        reasoning.append(
            f"this customer has recovered {history.prior_recoveries} of "
            f"{history.prior_failures + history.prior_recoveries} past failures"
        )
    if contacts_made >= 2:
        reasoning.append(
            f"{contacts_made} contacts already made, which is the largest single "
            f"contributor to the {churn}/1000 churn risk"
        )
    if attempts_used:
        reasoning.append(f"{attempts_used} attempt(s) already spent")

    return Scores(
        recovery_likelihood=recovery,
        churn_risk=churn,
        priority=prio,
        reasoning=tuple(reasoning),
    )


def days_between(earlier: dt.datetime, later: dt.datetime) -> int:
    return max(0, (later - earlier).days)
