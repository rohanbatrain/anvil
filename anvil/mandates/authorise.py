"""The single, total authorisation check.

Invariant 6 of ``docs/ARCHITECTURE.md``: no action executes without a valid
authorisation, and the check fails closed. This module is the one place in
Anvil that is allowed to return :data:`AuthorisationDecision.AUTHORISED`, and
it does so from exactly one statement at the very bottom of one function, after
every constraint has been evaluated. There is no early return that permits, no
``except`` that permits, and no default that permits. If a future check is added
and its branch is forgotten, the result is a denial, not a debit.

The check is *structural*, not statistical. Every branch is a comparison between
a number on the request and a number on a stored authorisation row. A model can
propose an action; it cannot influence the answer here, and it never sees this
code path.

Two decisions rather than one are needed on the deny side, because "no" and "not
yet" are commercially very different answers. An action that is inside the
principal's own mandate but outside the cap the principal delegated to an agent
is not an abuse -- it is the exact situation UPI Circle exists to handle, and the
right response is to ask the principal, not to abandon the money. That is
:data:`AuthorisationDecision.REQUIRES_STEP_UP`. Everything else that fails is
:data:`AuthorisationDecision.DENIED`, with the reason recorded.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from anvil.db.models.authorisation import Authorisation, AuthorisationUsage
from anvil.domain.enums import (
    ActionType,
    AuthorisationDecision,
    AuthorisationStatus,
    DenialReason,
)
from anvil.domain.money import Currency, Money
from anvil.mandates.cycles import is_as_presented


class StepUpTrigger(StrEnum):
    """Why a step-up is being asked for.

    The two triggers reach different people. A delegation overage asks the
    *principal* to extend the authority they granted; an issuer AFA demand asks
    the *customer* to re-authenticate on the rail. Conflating them would send
    the wrong message to the wrong person, so the outcome carries which it is.
    """

    DELEGATION_CAP_EXCEEDED = "delegation_cap_exceeded"
    ISSUER_DEMANDS_AFA = "issuer_demands_afa"


@dataclass(frozen=True, slots=True)
class AuthorisationCheck:
    """One evaluated constraint, kept whether it passed or failed.

    The trail is the point. An authorisation decision has to be provable after
    the fact -- to a merchant disputing a debit, to an auditor, to a judge
    reading the console -- and a bare ``DENIED`` proves nothing. Recording every
    comparison that was made, with the numbers it was made against, turns the
    decision into evidence.
    """

    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class AuthorisationRequest:
    """A proposed money-moving action, expressed in the terms the registry checks.

    Deliberately not the planner's action object: this carries only the facts an
    authorisation can be tested against, so nothing the model wrote can reach the
    comparison. ``amount`` must be positive, because an authorisation to move a
    non-positive sum is a category error rather than a bounded permission.
    """

    merchant_id: str
    customer_id: str
    action_type: ActionType
    amount: Money
    subscription_id: str | None = None

    #: The named delegate acting, when the authority being exercised is delegated.
    #: Must equal the authorisation's ``delegated_to_agent`` for it to apply.
    acting_agent: str | None = None

    #: Set from a prior decline classified ``AUTH_REQUIRED``. The rail, not Anvil,
    #: is demanding an additional factor.
    issuer_demands_afa: bool = False

    #: True when this presents the cycle's existing charge again, False when it
    #: raises a new one. A fixed-frequency mandate permits one presentation per
    #: cycle and a bounded number of retries of it; the caller has to say which
    #: this is, and the safe default is the one that gets refused.
    is_retry: bool = False

    #: Whether the principal can be asked, right now, to extend a delegated cap.
    #: An unattended overnight retry has nobody at the other end, so a delegation
    #: overage there is a denial rather than a challenge nobody will answer.
    principal_reachable: bool = True

    def __post_init__(self) -> None:
        if not self.merchant_id or not self.customer_id:
            raise ValueError("an authorisation request must name a merchant and a customer")
        if not self.amount.is_positive:
            raise ValueError(f"authorisation request amount must be positive, got {self.amount}")

    @property
    def currency(self) -> Currency:
        return self.amount.currency


@dataclass(frozen=True, slots=True)
class AuthorisationOutcome:
    """The answer, with the reasoning attached.

    ``effective_cap`` is the tightest single-debit ceiling that applied, after
    every limit was intersected. It is populated on denials too, because the most
    useful thing to tell a planner that asked for too much is how much it could
    have asked for -- that is what turns a refused debit into a split debit.
    """

    decision: AuthorisationDecision
    effective_cap: Money
    explanation: str
    authorisation_id: str | None = None
    denial_reason: DenialReason | None = None
    step_up_trigger: StepUpTrigger | None = None
    checks: tuple[AuthorisationCheck, ...] = field(default_factory=tuple)

    @property
    def is_authorised(self) -> bool:
        return self.decision is AuthorisationDecision.AUTHORISED

    @property
    def requires_step_up(self) -> bool:
        return self.decision is AuthorisationDecision.REQUIRES_STEP_UP

    @property
    def is_denied(self) -> bool:
        return self.decision is AuthorisationDecision.DENIED

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form for the audit record and the console."""
        return {
            "decision": self.decision.value,
            "authorisation_id": self.authorisation_id,
            "denial_reason": None if self.denial_reason is None else self.denial_reason.value,
            "step_up_trigger": (
                None if self.step_up_trigger is None else self.step_up_trigger.value
            ),
            "effective_cap_minor": self.effective_cap.minor,
            "currency": self.effective_cap.currency.value,
            "explanation": self.explanation,
            "checks": [c.to_dict() for c in self.checks],
        }


def denied_without_authorisation(
    request: AuthorisationRequest, *, detail: str | None = None
) -> AuthorisationOutcome:
    """The outcome when the customer holds nothing that could cover the action.

    Kept here rather than in the registry so that every ``DENIED`` in the system
    is constructed by this module, and the registry cannot accidentally invent a
    friendlier answer when it finds no rows.
    """
    reason = detail or (
        f"No stored authorisation covers {request.amount.format()} for customer "
        f"{request.customer_id}."
    )
    return AuthorisationOutcome(
        decision=AuthorisationDecision.DENIED,
        effective_cap=Money.zero(request.currency),
        explanation=f"Denied: {reason}",
        authorisation_id=None,
        denial_reason=DenialReason.NO_AUTHORISATION,
        checks=(AuthorisationCheck("authorisation_present", False, reason),),
    )


def authorise(
    request: AuthorisationRequest,
    auth: Authorisation,
    usage: AuthorisationUsage,
    now: dt.datetime,
) -> AuthorisationOutcome:
    """Decide whether ``auth`` permits ``request`` at ``now``, given ``usage``.

    Pure: it reads the three objects and returns a verdict, touching no database
    and mutating nothing. The caller records consumption separately and only if
    the action actually executes, so a check that is never acted on costs the
    mandate nothing.

    Constraints are evaluated in the order set out in section 8 of the
    architecture note, with one deliberate change: the counterparty check runs
    first rather than last. An authorisation belonging to another customer, bound
    to another subscription, held by another delegate, or denominated in another
    currency is not a *tighter* authorisation -- it is the wrong object, and every
    comparison that follows presupposes it is the right one.

    Raises ``ValueError`` only for a naive ``now``, which is a caller defect
    rather than an input condition. For every constructible request it returns
    one of the three decisions.
    """
    instant = _as_utc(now)
    checks: list[AuthorisationCheck] = []

    # --- 0. is this even the right authorisation? ---------------------------
    mismatch = _counterparty_mismatch(request, auth, usage)
    if mismatch is not None:
        checks.append(AuthorisationCheck("counterparty", False, mismatch))
        return _denied(
            auth, DenialReason.COUNTERPARTY_MISMATCH, mismatch, Money.zero(auth.currency), checks
        )
    checks.append(
        AuthorisationCheck(
            "counterparty",
            True,
            f"authorisation {auth.id} is held by customer {auth.customer_id} "
            f"under merchant {auth.merchant_id}",
        )
    )

    amount = request.amount
    cap = _effective_cap(auth, usage)

    # --- 1. status --------------------------------------------------------
    if auth.status is not AuthorisationStatus.ACTIVE:
        detail = f"authorisation status is {auth.status.value}, not active"
        checks.append(AuthorisationCheck("status", False, detail))
        return _denied(auth, DenialReason.STATUS_NOT_ACTIVE, detail, cap, checks)
    checks.append(AuthorisationCheck("status", True, "authorisation is active"))

    # --- 2. validity window ------------------------------------------------
    valid_from = _as_utc(auth.valid_from)
    valid_until = None if auth.valid_until is None else _as_utc(auth.valid_until)
    if instant < valid_from:
        detail = f"{_stamp(instant)} precedes valid_from {_stamp(valid_from)}"
        checks.append(AuthorisationCheck("validity_window", False, detail))
        return _denied(auth, DenialReason.OUTSIDE_VALIDITY_WINDOW, detail, cap, checks)
    if valid_until is not None and instant > valid_until:
        detail = f"{_stamp(instant)} is past valid_until {_stamp(valid_until)}"
        checks.append(AuthorisationCheck("validity_window", False, detail))
        return _denied(auth, DenialReason.OUTSIDE_VALIDITY_WINDOW, detail, cap, checks)
    checks.append(
        AuthorisationCheck(
            "validity_window",
            True,
            f"{_stamp(instant)} lies within {_stamp(valid_from)}.."
            f"{'open' if valid_until is None else _stamp(valid_until)}",
        )
    )

    # --- 3. the principal's single-debit ceiling ----------------------------
    if amount.minor > auth.max_amount_minor:
        detail = (
            f"{amount.format()} exceeds the mandate's single-debit ceiling of "
            f"{auth.max_amount.format()}"
        )
        checks.append(AuthorisationCheck("single_debit_cap", False, detail))
        return _denied(auth, DenialReason.AMOUNT_EXCEEDS_MANDATE, detail, cap, checks)
    checks.append(
        AuthorisationCheck(
            "single_debit_cap",
            True,
            f"{amount.format()} is within the mandate ceiling {auth.max_amount.format()}",
        )
    )

    # A pending step-up is carried rather than returned immediately. Asking a
    # customer to authenticate for an action that a later constraint would
    # refuse anyway is a wasted interruption and an eroded trust in the prompt.
    pending: StepUpTrigger | None = None
    pending_detail = ""

    # --- 4. the delegate's per-transaction cap -------------------------------
    agent_txn_cap = auth.agent_per_txn_cap_minor
    if agent_txn_cap is not None and amount.minor > agent_txn_cap:
        detail = (
            f"{amount.format()} is inside the principal's "
            f"{auth.max_amount.format()} mandate but above the "
            f"{Money(agent_txn_cap, auth.currency).format()} per-transaction cap delegated to "
            f"agent {auth.delegated_to_agent}"
        )
        checks.append(AuthorisationCheck("delegated_per_txn_cap", False, detail))
        if not request.principal_reachable:
            return _denied(
                auth,
                DenialReason.AMOUNT_EXCEEDS_DELEGATION,
                f"{detail}, and the principal cannot be asked to extend it for an "
                f"unattended action",
                cap,
                checks,
            )
        pending = StepUpTrigger.DELEGATION_CAP_EXCEEDED
        pending_detail = detail
    elif agent_txn_cap is not None:
        checks.append(
            AuthorisationCheck(
                "delegated_per_txn_cap",
                True,
                f"{amount.format()} is within the delegated per-transaction cap "
                f"{Money(agent_txn_cap, auth.currency).format()}",
            )
        )
    else:
        checks.append(
            AuthorisationCheck("delegated_per_txn_cap", True, "no delegated per-transaction cap")
        )

    # --- 5. the period cap over period_days ----------------------------------
    spent = usage.amount_debited_minor
    would_spend = spent + amount.minor
    principal_period_cap = auth.period_cap_minor
    agent_period_cap = auth.agent_period_cap_minor
    period_label = "the cycle" if auth.period_days is None else f"{auth.period_days} days"

    if principal_period_cap is not None and would_spend > principal_period_cap:
        detail = (
            f"{amount.format()} on top of {Money(spent, auth.currency).format()} already debited "
            f"would breach the {Money(principal_period_cap, auth.currency).format()} cap over "
            f"{period_label}"
        )
        checks.append(AuthorisationCheck("period_cap", False, detail))
        return _denied(auth, DenialReason.PERIOD_CAP_EXCEEDED, detail, cap, checks)
    if agent_period_cap is not None and would_spend > agent_period_cap:
        detail = (
            f"{amount.format()} on top of {Money(spent, auth.currency).format()} already debited "
            f"would breach the {Money(agent_period_cap, auth.currency).format()} period cap "
            f"delegated to agent {auth.delegated_to_agent} over {period_label}"
        )
        checks.append(AuthorisationCheck("period_cap", False, detail))
        if not request.principal_reachable:
            return _denied(auth, DenialReason.PERIOD_CAP_EXCEEDED, detail, cap, checks)
        if pending is None:
            pending = StepUpTrigger.DELEGATION_CAP_EXCEEDED
            pending_detail = detail
    else:
        checks.append(
            AuthorisationCheck(
                "period_cap",
                True,
                f"{Money(would_spend, auth.currency).format()} debited over {period_label} "
                f"remains within every period cap",
            )
        )

    # --- 6. frequency ---------------------------------------------------------
    cycle_start = _as_utc(usage.cycle_start)
    cycle_end = _as_utc(usage.cycle_end)
    if not cycle_start <= instant < cycle_end:
        detail = (
            f"{_stamp(instant)} falls outside the presented cycle "
            f"{_stamp(cycle_start)}..{_stamp(cycle_end)}"
        )
        checks.append(AuthorisationCheck("frequency", False, detail))
        return _denied(auth, DenialReason.FREQUENCY_VIOLATION, detail, cap, checks)
    if not is_as_presented(auth.frequency) and not request.is_retry and usage.attempts_used > 0:
        detail = (
            f"a {auth.frequency} mandate permits one presentation per cycle and this cycle "
            f"already carries {usage.attempts_used}"
        )
        checks.append(AuthorisationCheck("frequency", False, detail))
        return _denied(auth, DenialReason.FREQUENCY_VIOLATION, detail, cap, checks)
    checks.append(
        AuthorisationCheck(
            "frequency",
            True,
            f"{'retry of' if request.is_retry else 'first presentation in'} the cycle beginning "
            f"{_stamp(cycle_start)} is permitted at {auth.frequency} frequency",
        )
    )

    # --- 7. attempt allowance --------------------------------------------------
    if usage.attempts_used >= auth.max_attempts_per_cycle:
        detail = (
            f"{usage.attempts_used} of {auth.max_attempts_per_cycle} permitted attempts have "
            f"already been spent in this cycle"
        )
        checks.append(AuthorisationCheck("attempt_cap", False, detail))
        return _denied(auth, DenialReason.ATTEMPTS_EXHAUSTED, detail, cap, checks)
    checks.append(
        AuthorisationCheck(
            "attempt_cap",
            True,
            f"attempt {usage.attempts_used + 1} of {auth.max_attempts_per_cycle} in this cycle",
        )
    )

    # --- 8. Reserve Pay block --------------------------------------------------
    remaining = auth.remaining_block
    if auth.is_block and remaining is None:
        detail = "a Reserve Pay authorisation with no blocked amount cannot fund a debit"
        checks.append(AuthorisationCheck("reserve_block", False, detail))
        return _denied(auth, DenialReason.BLOCK_INSUFFICIENT, detail, cap, checks)
    if remaining is not None and amount.minor > remaining.minor:
        detail = (
            f"{amount.format()} exceeds the {remaining.format()} still undrawn on the "
            f"blocked amount"
        )
        checks.append(AuthorisationCheck("reserve_block", False, detail))
        return _denied(auth, DenialReason.BLOCK_INSUFFICIENT, detail, cap, checks)
    checks.append(
        AuthorisationCheck(
            "reserve_block",
            True,
            "not a blocked authorisation"
            if remaining is None
            else f"{amount.format()} is within the {remaining.format()} undrawn block",
        )
    )

    # --- 9. the issuer's own demand for an additional factor --------------------
    if request.issuer_demands_afa:
        detail = "the issuer requires an additional factor of authentication for this debit"
        checks.append(AuthorisationCheck("issuer_afa", False, detail))
        if pending is None:
            pending = StepUpTrigger.ISSUER_DEMANDS_AFA
            pending_detail = detail
    else:
        checks.append(AuthorisationCheck("issuer_afa", True, "no additional factor demanded"))

    if pending is not None:
        return AuthorisationOutcome(
            decision=AuthorisationDecision.REQUIRES_STEP_UP,
            effective_cap=cap,
            explanation=f"Step-up required: {pending_detail}.",
            authorisation_id=auth.id,
            denial_reason=None,
            step_up_trigger=pending,
            checks=tuple(checks),
        )

    return AuthorisationOutcome(
        decision=AuthorisationDecision.AUTHORISED,
        effective_cap=cap,
        explanation=(
            f"Authorised {amount.format()} against {auth.auth_type.value} authorisation "
            f"{auth.id}: attempt {usage.attempts_used + 1} of {auth.max_attempts_per_cycle} in "
            f"the cycle beginning {_stamp(cycle_start)}, effective ceiling "
            f"{cap.format()}."
        ),
        authorisation_id=auth.id,
        denial_reason=None,
        step_up_trigger=None,
        checks=tuple(checks),
    )


# --------------------------------------------------------------------------- helpers


def _denied(
    auth: Authorisation,
    reason: DenialReason,
    detail: str,
    cap: Money,
    checks: list[AuthorisationCheck],
) -> AuthorisationOutcome:
    return AuthorisationOutcome(
        decision=AuthorisationDecision.DENIED,
        effective_cap=cap,
        explanation=f"Denied ({reason.value}): {detail}.",
        authorisation_id=auth.id,
        denial_reason=reason,
        step_up_trigger=None,
        checks=tuple(checks),
    )


def _counterparty_mismatch(
    request: AuthorisationRequest, auth: Authorisation, usage: AuthorisationUsage
) -> str | None:
    """Return a description of the first identity mismatch, or None if it is the right row."""
    if auth.merchant_id != request.merchant_id:
        return (
            f"authorisation belongs to merchant {auth.merchant_id}, request is from "
            f"{request.merchant_id}"
        )
    if auth.customer_id != request.customer_id:
        return (
            f"authorisation belongs to customer {auth.customer_id}, request is for "
            f"{request.customer_id}"
        )
    if auth.subscription_id is not None and auth.subscription_id != request.subscription_id:
        return (
            f"authorisation is bound to subscription {auth.subscription_id}, request names "
            f"{request.subscription_id}"
        )
    if auth.delegated_to_agent is not None and auth.delegated_to_agent != request.acting_agent:
        return (
            f"authority is delegated to agent {auth.delegated_to_agent}; the request is acting "
            f"as {request.acting_agent or 'the merchant directly'}"
        )
    if auth.currency is not request.amount.currency:
        return (
            f"authorisation is denominated in {auth.currency.value}, request is in "
            f"{request.amount.currency.value}"
        )
    if usage.authorisation_id != auth.id:
        return f"usage row accounts against authorisation {usage.authorisation_id}, not {auth.id}"
    return None


def _effective_cap(auth: Authorisation, usage: AuthorisationUsage) -> Money:
    """The tightest single-debit ceiling in force, after intersecting every limit.

    Never negative: a period cap already overspent, or a block already drawn
    past, yields zero headroom rather than a negative ceiling that some caller
    would eventually treat as "unlimited".
    """
    cap = auth.max_amount
    if auth.agent_per_txn_cap_minor is not None:
        cap = cap.min(Money(auth.agent_per_txn_cap_minor, auth.currency))
    for period_cap in (auth.period_cap_minor, auth.agent_period_cap_minor):
        if period_cap is not None:
            cap = cap.min(Money(period_cap - usage.amount_debited_minor, auth.currency))
    remaining = auth.remaining_block
    if remaining is not None:
        cap = cap.min(remaining)
    elif auth.is_block:
        cap = Money.zero(auth.currency)
    return cap.max(Money.zero(auth.currency))


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        raise ValueError("Anvil refuses naive datetimes; pass a timezone-aware instant")
    return value.astimezone(dt.UTC)


def _stamp(value: dt.datetime) -> str:
    return _as_utc(value).strftime("%Y-%m-%d %H:%M UTC")
