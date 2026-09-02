"""The issuer model: ground truth for whether a debit settles.

This is the adversary the scheduler has to beat, and the integrity of every
number in the batch report depends on it being a fair one. Two design rules make
it fair rather than flattering.

**It generates from the same shape as the retry curves, with independent noise.**
If the simulator drew its success probabilities *from*
:mod:`anvil.domain.taxonomy`, the scheduler would be reading its own answer back
and a perfect score would prove nothing. Instead the issuer has its own
parameters -- its own salary-cycle amplitude, its own maintenance window, its own
per-bank quirks -- that agree with the taxonomy in shape but differ in detail,
and each simulated bank gets a persistent idiosyncratic offset. The scheduler is
therefore recovering real structure through noise, which is what the calibration
report in :mod:`anvil.risk.calibration` then measures honestly.

**Roughly one failure in five arrives as text no code table has seen.** Real
settlement systems emit free text written by whoever built them, and a simulator
that only ever produced clean enum values would make the LLM classifier look
unnecessary. The unmapped fraction is a tunable constant here, and the batch
report states what it was, so nobody has to take the split on trust.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from anvil.core.clock import ist_day_of_month, ist_hour
from anvil.domain.enums import AuthorisationType, FailureClass
from anvil.simulator.rng import (
    bernoulli,
    clamp_unit,
    substream,
    to_bps,
    uniform_between,
    weighted_choice,
)

#: Share of failures that carry a reason string no code table recognises.
#: Real-world figures vary by acquirer; a fifth is a defensible middle.
UNMAPPED_CODE_SHARE = Decimal("0.20")

#: Residual failure rate that no modelled cause explains -- fat fingers at the
#: acquirer, a switch hiccup, a race nobody reproduces. Small, but never zero.
IRREDUCIBLE_FAILURE = Decimal("0.015")

#: How hard a thin balance bites. Tuned against two anchors at once: a typical
#: customer on an ordinary day clears around 90%, which is where real
#: subscription debit success sits, while a customer who has already failed for
#: want of funds stays failing until their salary lands. The second anchor is
#: what makes the payday strategy worth anything -- with a mild balance term the
#: optimal policy is simply "retry soon and often", which is not the world.
BALANCE_SEVERITY = Decimal("0.80")


@dataclass(frozen=True, slots=True)
class Bank:
    """One simulated issuer, with a persistent personality.

    Banks differ, and consistently so: a bank with poor overnight availability
    has poor overnight availability every night. Baking that into a per-bank
    offset rather than redrawing it each attempt is what gives the classifier
    and the scheduler something real to learn.
    """

    code: str
    name: str
    #: Multiplier on the base settlement rate. Below 1.0 is a weak issuer.
    reliability: Decimal
    #: How much harder this bank is during the overnight window.
    maintenance_severity: Decimal
    #: Which dialect this bank writes its reason codes in.
    dialect: str

    def __str__(self) -> str:
        return self.name


#: A representative spread of Indian issuers by reliability and dialect. Names
#: are invented; the distribution of behaviour is the point, not the branding.
BANKS: tuple[Bank, ...] = (
    Bank("HDFC0", "Harbour National", Decimal("1.06"), Decimal("0.55"), "upi"),
    Bank("ICIC0", "Indus Commercial", Decimal("1.03"), Decimal("0.60"), "upi"),
    Bank("SBIN0", "State Union Bank", Decimal("0.92"), Decimal("0.35"), "nach"),
    Bank("PUNB0", "Peninsular Bank", Decimal("0.86"), Decimal("0.30"), "nach"),
    Bank("KKBK0", "Konkan Kotak", Decimal("1.04"), Decimal("0.65"), "upi"),
    Bank("AXIS0", "Axis Meridian", Decimal("1.01"), Decimal("0.58"), "card"),
    Bank("YESB0", "Yamuna Bank", Decimal("0.81"), Decimal("0.25"), "text"),
    Bank("IDIB0", "Deccan Grameen", Decimal("0.76"), Decimal("0.22"), "text"),
)


@dataclass(frozen=True, slots=True)
class DebitRequest:
    """One presentment, as the issuer sees it."""

    attempt_key: str
    at: dt.datetime
    amount_minor: int
    bank: Bank
    auth_type: AuthorisationType
    #: The customer's latent ability to pay, 0-1. Drives balance failures.
    ability_to_pay: Decimal
    #: True once the customer has revoked or paused the mandate.
    mandate_revoked: bool = False
    mandate_paused: bool = False
    #: True once the instrument has passed its expiry date.
    instrument_expired: bool = False
    account_closed: bool = False
    #: Attempts already presented in this cycle. Issuers get less tolerant.
    attempts_this_cycle: int = 0


@dataclass(frozen=True, slots=True)
class DebitOutcome:
    """What the issuer did, plus the truth about why.

    ``true_failure_class`` is ground truth and is never shown to the agent. The
    agent sees only ``raw_code`` and ``narration``, exactly as it would in
    production. The batch report compares the two to measure classification
    accuracy, which is only meaningful because the agent could not have seen it.
    """

    settled: bool
    true_failure_class: FailureClass | None = None
    raw_code: str | None = None
    narration: str | None = None
    #: Ex-ante probability this attempt settled. Feeds calibration.
    settle_probability_bps: int = 0
    #: True when the reason string is one no code table recognises.
    code_is_unmapped: bool = False


# ---------------------------------------------------------------------------
# Reason strings, by dialect
# ---------------------------------------------------------------------------

_MAPPED_CODES: dict[FailureClass, dict[str, tuple[str, ...]]] = {
    FailureClass.INSUFFICIENT_FUNDS: {
        "upi": ("Z9",),
        "nach": ("01",),
        "card": ("51",),
        "text": ("insufficient_funds",),
    },
    FailureClass.ISSUER_TECHNICAL: {
        "upi": ("U30", "U28", "U67"),
        "nach": ("14", "26"),
        "card": ("91", "96"),
        "text": ("gateway_error",),
    },
    FailureClass.INSTRUMENT_EXPIRED: {
        "upi": ("U69",),
        "nach": ("13",),
        "card": ("54",),
        "text": ("card_expired",),
    },
    FailureClass.LIMIT_EXCEEDED: {
        "upi": ("B3",),
        "nach": ("12",),
        "card": ("61", "65"),
        "text": ("limit_exceeded",),
    },
    FailureClass.MANDATE_REVOKED: {
        "upi": ("U69",),
        "nach": ("05", "10"),
        "card": ("54",),
        "text": ("mandate_cancelled",),
    },
    FailureClass.MANDATE_PAUSED: {
        "upi": ("Z7",),
        "nach": ("08", "22"),
        "card": ("62",),
        "text": ("mandate_on_hold",),
    },
    FailureClass.ACCOUNT_CLOSED: {
        "upi": ("XH", "YA"),
        "nach": ("02", "11"),
        "card": ("14", "41"),
        "text": ("account_closed",),
    },
    FailureClass.RISK_DECLINED: {
        "upi": ("U16", "ZA"),
        "nach": ("26",),
        "card": ("05", "59"),
        "text": ("risk_declined",),
    },
    FailureClass.AUTH_REQUIRED: {
        "upi": ("ZM", "AM"),
        "nach": ("14",),
        "card": ("78",),
        "text": ("afa_required",),
    },
}

#: Free text a settlement system might actually emit. None of these resolve
#: through the deterministic tables, which is exactly why they exist.
_UNMAPPED_STRINGS: dict[FailureClass, tuple[str, ...]] = {
    FailureClass.INSUFFICIENT_FUNDS: (
        "A/c bal low",
        "Insuff. bal in acct",
        "BALANCE NOT SUFFICIENT",
        "funds shortage at remitter",
        "acct balance below txn amt",
    ),
    FailureClass.ISSUER_TECHNICAL: (
        "Remitter CBS down",
        "host unreachable, pls retry",
        "TIMEOUT AT BANK END",
        "switch busy",
        "unable to process at this time",
    ),
    FailureClass.INSTRUMENT_EXPIRED: (
        "card validity over",
        "instrument past expiry",
        "EXPD CARD",
    ),
    FailureClass.LIMIT_EXCEEDED: (
        "per txn cap breached",
        "daily limit over",
        "AMT EXCEEDS MANDATE CAP",
    ),
    FailureClass.MANDATE_REVOKED: (
        "customer cancelled standing instruction",
        "SI withdrawn by drawer",
        "umn not active",
    ),
    FailureClass.MANDATE_PAUSED: ("stop payment instruction", "SI on hold by customer"),
    FailureClass.ACCOUNT_CLOSED: ("acct closed", "NO SUCH ACCOUNT", "dormant account"),
    FailureClass.RISK_DECLINED: ("declined by issuer risk", "REFER TO ISSUER"),
    FailureClass.AUTH_REQUIRED: ("AFA pending", "customer authentication needed"),
}


# ---------------------------------------------------------------------------
# The issuer
# ---------------------------------------------------------------------------


class Issuer:
    """A seeded issuer. Deterministic: same seed and request give the same answer."""

    __slots__ = ("_seed",)

    def __init__(self, seed: int) -> None:
        self._seed = seed

    @property
    def seed(self) -> int:
        return self._seed

    # -- the generative parameters, deliberately not imported from taxonomy ---

    @staticmethod
    def _salary_factor(day: int) -> Decimal:
        """Balance availability by day of month.

        Agrees in shape with the taxonomy's salary curve -- high at the turn of
        the month, thin around the 20th -- while differing in amplitude and in
        exactly where the trough sits. That mismatch is the point.
        """
        if day >= 29 or day <= 2:
            return Decimal("1.38")
        if day <= 5:
            return Decimal("1.22")
        if day <= 9:
            return Decimal("1.02")
        if day <= 14:
            return Decimal("0.88")
        if day <= 21:
            return Decimal("0.71")
        if day <= 25:
            return Decimal("0.79")
        return Decimal("1.05")

    @staticmethod
    def _rail_availability(hour: int, severity: Decimal) -> Decimal:
        """Probability the rail can carry a presentment at this hour, 0-1.

        The overnight dip is the NPCI settlement and issuer maintenance window.
        Expressed as availability rather than as a multiplier so it can be read
        directly as "this bank answers 45% of the time at 2am", which is a
        statement someone can check against an operations dashboard.
        """
        if 1 <= hour <= 4:
            return clamp_unit(Decimal("1.00") - severity)
        if hour in (0, 5):
            return clamp_unit(Decimal("1.00") - severity / 2)
        if 9 <= hour <= 19:
            return Decimal("0.985")
        return Decimal("0.965")

    def _idiosyncrasy(self, bank: Bank, day: int) -> Decimal:
        """A persistent per-bank, per-day wobble the scheduler cannot know.

        Redrawn per (bank, day) rather than per attempt, so it behaves like a
        real operational condition rather than white noise -- which makes it
        genuinely hard to fit, in the way real data is.
        """
        rng = substream(self._seed, "issuer", "wobble", bank.code, str(day))
        # Deliberately narrow. Wide day-to-day noise would drown the salary-cycle
        # and maintenance-window structure the scheduler is supposed to find,
        # and a simulator whose noise exceeds its signal cannot tell a good
        # scheduler from a lucky one.
        return uniform_between(rng, Decimal("0.96"), Decimal("1.04"))

    # -- the decision ---------------------------------------------------------

    def settle_probability(self, request: DebitRequest) -> Decimal:
        """P(this presentment settles), before the coin is flipped.

        Terminal conditions short-circuit to zero: a revoked mandate does not
        settle with 3% probability, it does not settle.
        """
        if (
            request.mandate_revoked
            or request.account_closed
            or request.instrument_expired
            or request.mandate_paused
        ):
            return Decimal(0)

        day = ist_day_of_month(request.at)
        hour = ist_hour(request.at)

        # An additive hazard model rather than a product of multipliers. Each
        # term is the probability that one specific thing goes wrong, so the
        # numbers can be reasoned about and argued with individually -- and a
        # healthy debit lands near 1.0 by default rather than by cancellation.

        # Balance. The exponent makes the penalty bite hard only at the thin
        # end: a customer at 0.9 barely notices the cycle, one at 0.2 is
        # governed by it. (2 - cycle) inverts the salary curve into a hazard.
        headroom = clamp_unit(request.ability_to_pay)
        cycle = self._salary_factor(day)
        shortfall = (Decimal(1) - headroom) ** Decimal("1.5")
        balance_hazard = shortfall * (Decimal(2) - cycle) * BALANCE_SEVERITY

        # Rail. Unavailability plus a penalty for a structurally weaker issuer.
        availability = self._rail_availability(hour, request.bank.maintenance_severity)
        rail_hazard = (Decimal(1) - availability) + max(
            Decimal(0), (Decimal("1.10") - request.bank.reliability)
        ) * Decimal("0.30")

        # Repeat presentments. Issuers get less tolerant within one cycle.
        fatigue_hazard = Decimal(request.attempts_this_cycle) * Decimal("0.04")

        hazard = balance_hazard + rail_hazard + fatigue_hazard + IRREDUCIBLE_FAILURE
        probability = clamp_unit(Decimal(1) - hazard)

        # The per-bank, per-day wobble sits on the survival probability, so a
        # bad day at a bank shifts everything a little without ever making a
        # terminal case succeed.
        return clamp_unit(probability * self._idiosyncrasy(request.bank, day))

    def present(self, request: DebitRequest) -> DebitOutcome:
        """Decide the attempt, and if it fails, say why in the bank's own dialect."""
        probability = self.settle_probability(request)
        rng = substream(self._seed, "issuer", "present", request.attempt_key)

        if bernoulli(rng, probability):
            return DebitOutcome(settled=True, settle_probability_bps=to_bps(probability))

        failure_class = self._failure_class(request, rng)
        raw_code, narration, unmapped = self._reason(failure_class, request.bank, rng)
        return DebitOutcome(
            settled=False,
            true_failure_class=failure_class,
            raw_code=raw_code,
            narration=narration,
            settle_probability_bps=to_bps(probability),
            code_is_unmapped=unmapped,
        )

    def _failure_class(self, request: DebitRequest, rng) -> FailureClass:  # type: ignore[no-untyped-def]
        """Which way it failed, given that it failed.

        Terminal states are checked first because they are facts about the
        world, not draws. Everything else is weighted by how plausible it is for
        this customer at this moment: a customer with no money mostly fails for
        no money.
        """
        if request.mandate_revoked:
            return FailureClass.MANDATE_REVOKED
        if request.account_closed:
            return FailureClass.ACCOUNT_CLOSED
        if request.instrument_expired:
            return FailureClass.INSTRUMENT_EXPIRED
        if request.mandate_paused:
            return FailureClass.MANDATE_PAUSED

        poverty = int((Decimal(1) - clamp_unit(request.ability_to_pay)) * 100)
        weak_rail = int((Decimal("1.10") - request.bank.reliability) * 100)
        return weighted_choice(
            rng,
            [
                (FailureClass.INSUFFICIENT_FUNDS, 20 + poverty * 2),
                (FailureClass.ISSUER_TECHNICAL, 22 + max(0, weak_rail) * 3),
                (FailureClass.LIMIT_EXCEEDED, 8),
                (FailureClass.RISK_DECLINED, 5),
                (FailureClass.AUTH_REQUIRED, 4),
                (FailureClass.UNKNOWN, 3),
            ],
        )

    def _reason(
        self,
        failure_class: FailureClass,
        bank: Bank,
        rng,  # type: ignore[no-untyped-def]
    ) -> tuple[str, str, bool]:
        """A reason code and narration, in this bank's dialect.

        With probability :data:`UNMAPPED_CODE_SHARE` the code is free text no
        table recognises, which forces the LLM classifier to earn its place.
        """
        unmapped_pool = _UNMAPPED_STRINGS.get(failure_class)
        if unmapped_pool and bernoulli(rng, UNMAPPED_CODE_SHARE):
            text = unmapped_pool[rng.randrange(len(unmapped_pool))]
            return text, f"{bank.name}: {text}", True

        by_dialect = _MAPPED_CODES.get(failure_class)
        if by_dialect is None:
            return "", f"{bank.name}: declined", True
        codes = by_dialect.get(bank.dialect) or next(iter(by_dialect.values()))
        code = codes[rng.randrange(len(codes))]
        return code, f"{bank.name}: {failure_class.value.replace('_', ' ')} [{code}]", False

    def pick_bank(self, customer_id: str) -> Bank:
        """A customer's bank. Stable for the life of the world."""
        rng = substream(self._seed, "issuer", "bank", customer_id)
        return weighted_choice(
            rng,
            [
                (BANKS[0], 22),
                (BANKS[1], 18),
                (BANKS[2], 20),
                (BANKS[3], 12),
                (BANKS[4], 10),
                (BANKS[5], 9),
                (BANKS[6], 5),
                (BANKS[7], 4),
            ],
        )
