"""Decline-code taxonomy and retry hazard curves.

Two responsibilities, both deliberately free of any model call:

1. Map a raw issuer / NPCI / NACH / card reason code onto a
   :class:`~anvil.domain.enums.FailureClass`. Roughly four in five real failures
   carry a code we recognise; those resolve here, instantly and reproducibly.
   Only the unrecognised remainder reaches the LLM classifier -- and even then
   the model must answer inside this same closed enum.

2. Answer "if I retry this, what is the chance it settles, and when is the best
   moment?" with a tabulated hazard function. This is a statistical estimation
   problem with abundant labelled data, so it is solved with arithmetic. Handing
   it to a language model would be slower, worse, and non-reproducible -- it is
   the canonical mistake this track penalises, and Anvil does not make it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from anvil.domain.enums import FailureClass, RetryPosture

# ---------------------------------------------------------------------------
# 1. Raw code -> FailureClass
# ---------------------------------------------------------------------------
# Sources are heterogeneous by design: UPI/NPCI response codes, NACH return
# reason codes, ISO-8583 card response codes, and Razorpay's own error strings
# all describe the same handful of underlying realities in different dialects.

_UPI_CODES: dict[str, FailureClass] = {
    "U30": FailureClass.ISSUER_TECHNICAL,  # debit failure at remitter bank
    "U31": FailureClass.ISSUER_TECHNICAL,  # credit failure at beneficiary
    "U16": FailureClass.RISK_DECLINED,  # risk threshold exceeded
    "U28": FailureClass.ISSUER_TECHNICAL,  # remitter bank unavailable
    "U54": FailureClass.ISSUER_TECHNICAL,  # transaction timed out
    "U67": FailureClass.ISSUER_TECHNICAL,  # debit timeout
    "U69": FailureClass.MANDATE_REVOKED,  # mandate not found / deregistered
    "Z9": FailureClass.INSUFFICIENT_FUNDS,  # insufficient balance
    "Z6": FailureClass.LIMIT_EXCEEDED,  # number of PIN tries exceeded
    "Z7": FailureClass.RISK_DECLINED,
    "ZA": FailureClass.RISK_DECLINED,  # transaction declined by customer
    "ZM": FailureClass.AUTH_REQUIRED,  # invalid / required MPIN
    "XH": FailureClass.ACCOUNT_CLOSED,  # account does not exist
    "XD": FailureClass.ACCOUNT_CLOSED,  # invalid account
    "XF": FailureClass.ACCOUNT_CLOSED,  # format error / account invalid
    "XT": FailureClass.ISSUER_TECHNICAL,
    "YA": FailureClass.ACCOUNT_CLOSED,  # account blocked
    "YB": FailureClass.ISSUER_TECHNICAL,
    "B3": FailureClass.LIMIT_EXCEEDED,  # per-transaction limit
    "AM": FailureClass.AUTH_REQUIRED,  # MPIN not set
}

_NACH_RETURN_CODES: dict[str, FailureClass] = {
    "01": FailureClass.INSUFFICIENT_FUNDS,  # funds insufficient
    "02": FailureClass.ACCOUNT_CLOSED,
    "03": FailureClass.ACCOUNT_CLOSED,  # no such account
    "05": FailureClass.MANDATE_REVOKED,  # not arranged for / mandate cancelled
    "08": FailureClass.MANDATE_PAUSED,  # payment stopped by drawer
    "09": FailureClass.ACCOUNT_CLOSED,  # account frozen
    "10": FailureClass.MANDATE_REVOKED,  # mandate not registered
    "11": FailureClass.ACCOUNT_CLOSED,  # account blocked / dormant
    "12": FailureClass.LIMIT_EXCEEDED,  # amount exceeds mandate limit
    "13": FailureClass.MANDATE_REVOKED,  # mandate expired
    "14": FailureClass.ISSUER_TECHNICAL,  # technical reasons
    "22": FailureClass.MANDATE_PAUSED,  # mandate on hold
    "26": FailureClass.ISSUER_TECHNICAL,
}

_CARD_ISO_CODES: dict[str, FailureClass] = {
    "04": FailureClass.ACCOUNT_CLOSED,  # pick up card
    "05": FailureClass.RISK_DECLINED,  # do not honour
    "12": FailureClass.RISK_DECLINED,  # invalid transaction
    "14": FailureClass.ACCOUNT_CLOSED,  # invalid card number
    "41": FailureClass.ACCOUNT_CLOSED,  # lost card
    "43": FailureClass.ACCOUNT_CLOSED,  # stolen card
    "51": FailureClass.INSUFFICIENT_FUNDS,
    "54": FailureClass.INSTRUMENT_EXPIRED,
    "57": FailureClass.RISK_DECLINED,  # transaction not permitted
    "59": FailureClass.RISK_DECLINED,  # suspected fraud
    "61": FailureClass.LIMIT_EXCEEDED,  # exceeds withdrawal amount limit
    "62": FailureClass.RISK_DECLINED,  # restricted card
    "65": FailureClass.LIMIT_EXCEEDED,  # exceeds withdrawal frequency
    "78": FailureClass.AUTH_REQUIRED,  # card not activated
    "82": FailureClass.RISK_DECLINED,  # CVV failure
    "91": FailureClass.ISSUER_TECHNICAL,  # issuer unavailable
    "92": FailureClass.ISSUER_TECHNICAL,
    "96": FailureClass.ISSUER_TECHNICAL,  # system malfunction
}

_TEXTUAL_CODES: dict[str, FailureClass] = {
    "insufficient_funds": FailureClass.INSUFFICIENT_FUNDS,
    "insufficient_balance": FailureClass.INSUFFICIENT_FUNDS,
    "card_expired": FailureClass.INSTRUMENT_EXPIRED,
    "expired_card": FailureClass.INSTRUMENT_EXPIRED,
    "payment_declined_by_bank": FailureClass.RISK_DECLINED,
    "payment_failed": FailureClass.UNKNOWN,
    "gateway_error": FailureClass.ISSUER_TECHNICAL,
    "server_error": FailureClass.ISSUER_TECHNICAL,
    "issuer_down": FailureClass.ISSUER_TECHNICAL,
    "bank_down": FailureClass.ISSUER_TECHNICAL,
    "mandate_cancelled": FailureClass.MANDATE_REVOKED,
    "mandate_revoked": FailureClass.MANDATE_REVOKED,
    "umn_not_found": FailureClass.MANDATE_REVOKED,
    "mandate_on_hold": FailureClass.MANDATE_PAUSED,
    "mandate_paused": FailureClass.MANDATE_PAUSED,
    "account_closed": FailureClass.ACCOUNT_CLOSED,
    "account_blocked": FailureClass.ACCOUNT_CLOSED,
    "invalid_account": FailureClass.ACCOUNT_CLOSED,
    "limit_exceeded": FailureClass.LIMIT_EXCEEDED,
    "amount_exceeds_limit": FailureClass.LIMIT_EXCEEDED,
    "authentication_required": FailureClass.AUTH_REQUIRED,
    "afa_required": FailureClass.AUTH_REQUIRED,
    "step_up_required": FailureClass.AUTH_REQUIRED,
    "risk_declined": FailureClass.RISK_DECLINED,
    "fraud_suspected": FailureClass.RISK_DECLINED,
}

#: Namespaced lookup. Callers who know the rail should say so; callers who do
#: not can use :func:`classify_code`, which searches every namespace.
CODE_NAMESPACES: dict[str, dict[str, FailureClass]] = {
    "upi": _UPI_CODES,
    "nach": _NACH_RETURN_CODES,
    "card": _CARD_ISO_CODES,
    "text": _TEXTUAL_CODES,
}


def normalise_code(raw: str) -> str:
    """Fold a raw code into its lookup form.

    ``"NPCI:U30 debit failed"`` -> ``"u30"``; ``"Insufficient Funds"`` ->
    ``"insufficient_funds"``. Deliberately conservative: it only strips and
    folds, it never guesses.
    """
    token = raw.strip().lower()
    for prefix in ("npci:", "npci ", "upi:", "nach:", "rc ", "rc:", "code:", "error:"):
        if token.startswith(prefix):
            token = token[len(prefix) :].strip()
    token = (
        token.split()[0] if token and " " in token and _looks_like_code(token.split()[0]) else token
    )
    return token.replace(" ", "_").replace("-", "_").strip("_.,;:")


def _looks_like_code(token: str) -> bool:
    """A short alphanumeric token such as ``u30`` or ``51`` reads as a code."""
    return 1 <= len(token) <= 4 and any(c.isdigit() for c in token)


def classify_code(raw: str, *, namespace: str | None = None) -> FailureClass | None:
    """Deterministically map a raw reason code to a failure class.

    Returns ``None`` when the code is unrecognised -- the caller then escalates
    to the LLM classifier. Returning ``None`` rather than ``UNKNOWN`` keeps
    "we have never seen this" distinct from "we looked and it is genuinely
    unclassifiable".
    """
    if not raw or not raw.strip():
        return None
    token = normalise_code(raw)
    if namespace is not None:
        return CODE_NAMESPACES[namespace].get(token) or CODE_NAMESPACES[namespace].get(
            token.upper()
        )
    for table in (_UPI_CODES, _NACH_RETURN_CODES, _CARD_ISO_CODES):
        hit = table.get(token.upper())
        if hit is not None:
            return hit
    return _TEXTUAL_CODES.get(token)


def known_codes() -> dict[str, list[str]]:
    """Every code the deterministic path recognises, by namespace. Used in docs
    and by the coverage test that asserts the tables have not silently shrunk."""
    return {ns: sorted(table) for ns, table in CODE_NAMESPACES.items()}


# ---------------------------------------------------------------------------
# 2. Retry hazard curves
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetryCurve:
    """A discrete hazard function for one failure class.

    ``probability`` composes four independent factors, each a plain lookup:

    * ``attempt_base`` -- how likely attempt *n* is to clear at all,
    * an **age** factor -- how the chance evolves with hours since the failure,
    * a **circadian** factor -- issuer maintenance windows and settlement cycles,
    * a **salary-cycle** factor -- only meaningful for balance-driven failures.

    Everything is a Decimal, so the same inputs give the same output on every
    machine. Nothing here is learned at runtime; in offline mode the parameters
    are the simulator's own generative constants, which is what lets the
    evidence harness prove the scheduler recovers real signal rather than noise.
    """

    failure_class: FailureClass
    posture: RetryPosture
    #: P(settle) for attempt 1, 2, 3, ... Empty means "never retry".
    attempt_base: tuple[Decimal, ...]
    #: Multiplier keyed by whole hours since the original failure.
    age_factors: tuple[tuple[int, Decimal], ...] = ()
    #: True when the balance-driven salary-cycle factor applies.
    salary_sensitive: bool = False
    #: True when issuer maintenance windows matter.
    circadian_sensitive: bool = True
    #: Human-readable justification, surfaced in the UI next to the schedule.
    rationale: str = ""

    @property
    def max_attempts(self) -> int:
        return len(self.attempt_base)

    @property
    def is_retryable(self) -> bool:
        return self.max_attempts > 0

    def probability(
        self,
        *,
        attempt: int,
        hours_since_failure: int,
        hour_of_day: int,
        day_of_month: int,
    ) -> Decimal:
        """P(this attempt settles), clamped to [0, 1].

        ``attempt`` is 1-based. Attempts beyond ``max_attempts`` return zero,
        which is what makes the attempt cap a property of the curve rather than
        a separate rule someone can forget to apply.
        """
        if attempt < 1 or attempt > self.max_attempts:
            return Decimal(0)
        if not 0 <= hour_of_day <= 23:
            raise ValueError(f"hour_of_day out of range: {hour_of_day}")
        if not 1 <= day_of_month <= 31:
            raise ValueError(f"day_of_month out of range: {day_of_month}")

        p = self.attempt_base[attempt - 1]
        p *= self._age_factor(hours_since_failure)
        if self.circadian_sensitive:
            p *= _CIRCADIAN[hour_of_day]
        if self.salary_sensitive:
            p *= _SALARY_CYCLE[day_of_month]
        return min(Decimal(1), max(Decimal(0), p))

    def _age_factor(self, hours: int) -> Decimal:
        if not self.age_factors:
            return Decimal(1)
        chosen = self.age_factors[0][1]
        for threshold, factor in self.age_factors:
            if hours >= threshold:
                chosen = factor
            else:
                break
        return chosen


#: Hour-of-day multiplier, IST. Overnight is the NPCI/issuer maintenance and
#: settlement window -- retrying at 02:00 wastes an attempt. Late morning and
#: early evening are the strongest.
_CIRCADIAN: dict[int, Decimal] = {
    0: Decimal("0.55"),
    1: Decimal("0.40"),
    2: Decimal("0.30"),
    3: Decimal("0.35"),
    4: Decimal("0.55"),
    5: Decimal("0.75"),
    6: Decimal("0.90"),
    7: Decimal("1.00"),
    8: Decimal("1.05"),
    9: Decimal("1.10"),
    10: Decimal("1.15"),
    11: Decimal("1.15"),
    12: Decimal("1.10"),
    13: Decimal("1.05"),
    14: Decimal("1.05"),
    15: Decimal("1.08"),
    16: Decimal("1.10"),
    17: Decimal("1.12"),
    18: Decimal("1.12"),
    19: Decimal("1.08"),
    20: Decimal("1.05"),
    21: Decimal("1.00"),
    22: Decimal("0.90"),
    23: Decimal("0.72"),
}

#: Day-of-month multiplier for balance-driven failures. Indian salary credits
#: cluster on the last working day and the first of the month; balances are at
#: their thinnest in the days just before.
_SALARY_CYCLE: dict[int, Decimal] = {
    1: Decimal("1.45"),
    2: Decimal("1.40"),
    3: Decimal("1.30"),
    4: Decimal("1.18"),
    5: Decimal("1.10"),
    6: Decimal("1.02"),
    7: Decimal("0.96"),
    8: Decimal("0.92"),
    9: Decimal("0.88"),
    10: Decimal("0.86"),
    11: Decimal("0.84"),
    12: Decimal("0.82"),
    13: Decimal("0.80"),
    14: Decimal("0.78"),
    15: Decimal("0.80"),
    16: Decimal("0.78"),
    17: Decimal("0.75"),
    18: Decimal("0.72"),
    19: Decimal("0.70"),
    20: Decimal("0.68"),
    21: Decimal("0.66"),
    22: Decimal("0.64"),
    23: Decimal("0.64"),
    24: Decimal("0.68"),
    25: Decimal("0.80"),
    26: Decimal("0.95"),
    27: Decimal("1.10"),
    28: Decimal("1.25"),
    29: Decimal("1.35"),
    30: Decimal("1.42"),
    31: Decimal("1.45"),
}


def _curve(
    fc: FailureClass,
    posture: RetryPosture,
    base: list[str],
    *,
    age: list[tuple[int, str]] | None = None,
    salary: bool = False,
    circadian: bool = True,
    rationale: str = "",
) -> RetryCurve:
    return RetryCurve(
        failure_class=fc,
        posture=posture,
        attempt_base=tuple(Decimal(b) for b in base),
        age_factors=tuple((h, Decimal(f)) for h, f in (age or [])),
        salary_sensitive=salary,
        circadian_sensitive=circadian,
        rationale=rationale,
    )


RETRY_CURVES: dict[FailureClass, RetryCurve] = {
    FailureClass.INSUFFICIENT_FUNDS: _curve(
        FailureClass.INSUFFICIENT_FUNDS,
        RetryPosture.RETRY_SCHEDULED,
        ["0.34", "0.28", "0.21", "0.12"],
        age=[(0, "0.55"), (12, "0.85"), (24, "1.00"), (72, "1.12"), (168, "0.95"), (336, "0.70")],
        salary=True,
        rationale=(
            "Balance recovers on a payday rhythm, not a clock. Retrying an hour later "
            "almost always fails; retrying on the 1st or the last working day is where "
            "the money is. Timing dominates attempt count for this class."
        ),
    ),
    FailureClass.ISSUER_TECHNICAL: _curve(
        FailureClass.ISSUER_TECHNICAL,
        RetryPosture.RETRY_FAST,
        ["0.62", "0.48", "0.30"],
        age=[(0, "0.70"), (2, "1.00"), (6, "1.10"), (24, "0.95"), (72, "0.75")],
        rationale=(
            "The customer could always pay; the rail could not take the money. These are "
            "the cheapest recoveries in the book -- retry within hours, outside the "
            "overnight maintenance window, and most of them simply settle."
        ),
    ),
    FailureClass.LIMIT_EXCEEDED: _curve(
        FailureClass.LIMIT_EXCEEDED,
        RetryPosture.RETRY_SCHEDULED,
        ["0.41", "0.33", "0.18"],
        age=[(0, "0.30"), (24, "1.00"), (48, "1.05")],
        rationale=(
            "Per-transaction and per-day caps reset on a daily or monthly boundary. "
            "Waiting for the reset is the whole strategy; splitting the debit is the "
            "alternative when the cap is per-transaction rather than per-period."
        ),
    ),
    FailureClass.MANDATE_PAUSED: _curve(
        FailureClass.MANDATE_PAUSED,
        RetryPosture.DEFERRED,
        ["0.22", "0.15"],
        age=[(0, "0.40"), (72, "1.00"), (336, "0.60")],
        rationale=(
            "The customer deliberately paused this. Retrying against a hold is close to "
            "useless -- the recovery path is a conversation, not an attempt."
        ),
    ),
    FailureClass.UNKNOWN: _curve(
        FailureClass.UNKNOWN,
        RetryPosture.RETRY_ONCE,
        ["0.25"],
        age=[(0, "0.60"), (12, "1.00")],
        rationale=(
            "One conservative attempt buys information cheaply. After that a human "
            "looks at it, because guessing repeatedly against an unmapped code is how "
            "issuer relationships get damaged."
        ),
    ),
    FailureClass.AUTH_REQUIRED: _curve(
        FailureClass.AUTH_REQUIRED,
        RetryPosture.DEFERRED,
        [],
        rationale=(
            "Retrying cannot succeed until the customer re-authenticates. The action is "
            "an AFA step-up, and the graph waits on it."
        ),
    ),
    FailureClass.INSTRUMENT_EXPIRED: _curve(
        FailureClass.INSTRUMENT_EXPIRED,
        RetryPosture.NEVER,
        [],
        rationale=(
            "The card will be just as expired tomorrow. The only recovery is a new "
            "instrument, so every retry attempt spent here is pure waste."
        ),
    ),
    FailureClass.MANDATE_REVOKED: _curve(
        FailureClass.MANDATE_REVOKED,
        RetryPosture.NEVER,
        [],
        rationale=(
            "There is no longer an authorisation to debit against. Attempting anyway is "
            "not merely futile, it is unauthorised. Re-authorisation only."
        ),
    ),
    FailureClass.ACCOUNT_CLOSED: _curve(
        FailureClass.ACCOUNT_CLOSED,
        RetryPosture.NEVER,
        [],
        rationale="The account cannot receive a debit. Instrument change, or churn.",
    ),
    FailureClass.RISK_DECLINED: _curve(
        FailureClass.RISK_DECLINED,
        RetryPosture.NEVER,
        [],
        rationale=(
            "Retrying a risk decline is actively harmful: repeated attempts worsen the "
            "merchant's issuer risk score and can get the descriptor blocked outright. "
            "This is the one class where doing nothing beats doing something."
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class RecoveryPosture:
    """Everything the planner needs to know about a class, in one object."""

    failure_class: FailureClass
    curve: RetryCurve
    #: Actions that make sense for this class, as a hint to the planner. The
    #: policy engine remains the authority; this only shapes the proposal.
    suggested_actions: tuple[str, ...] = field(default=())

    @property
    def is_terminal_for_debit(self) -> bool:
        return self.curve.posture is RetryPosture.NEVER


def posture_for(failure_class: FailureClass) -> RecoveryPosture:
    return RecoveryPosture(failure_class=failure_class, curve=RETRY_CURVES[failure_class])


def is_retryable(failure_class: FailureClass) -> bool:
    return RETRY_CURVES[failure_class].is_retryable


def max_attempts_for(failure_class: FailureClass) -> int:
    return RETRY_CURVES[failure_class].max_attempts
