"""The typed fact set a policy evaluation sees.

A policy decision is only as replayable as the inputs it was made from. This
module defines the *entire* surface a rule may test -- nothing else is visible
to the expression language -- and it defines it as a frozen, JSON-serialisable
model so ``PolicyEvaluation.facts`` can store byte-for-byte what was evaluated.
Months later, "why was this allowed?" is answered by re-running the same bundle
against the stored row and getting the same decision.

Three deliberate choices shape the design:

*Everything is a JSON scalar.* Fields are ``int``, ``bool``, or a closed-vocabulary
string. There are no floats anywhere (invariant 3), no nested objects, and no
datetimes -- the instant is reduced to the facts that actually matter to a rule
(the IST hour, hours elapsed) so that every comparison in the expression language
is a total ordering over integers.

*There are no optional numbers.* An absent measurement would force ordered
comparisons to invent a truth value for ``None``, and a rule that quietly fails
to match is a rule that quietly allows. "Never contacted" is therefore
:data:`NEVER_CONTACTED_HOURS`, a sentinel large enough that every cooling-off
comparison behaves correctly, paired with an explicit boolean.

*Relations between facts are precomputed.* The expression language compares a
field to a literal, never a field to another field -- that keeps evaluation
trivially total and auditable. Where a rule genuinely needs a relation ("is this
concession bigger than the remaining budget?"), the relation is computed here,
once, and stored as its own boolean fact. It is then visible in the persisted
row, which is exactly where a reviewer wants to see it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from anvil.domain.enums import (
    ActionType,
    AuthorisationDecision,
    ConsentState,
    FailureClass,
    MessagePurpose,
    RetryPosture,
)
from anvil.domain.money import Currency
from anvil.domain.taxonomy import RETRY_CURVES

#: Sentinel for ``hours_since_last_contact`` when the customer has never been
#: contacted. One year, which is longer than any cooling-off window a merchant
#: could sanely write, so ``hours_since_last_contact < N`` is false exactly when
#: it should be. Paired with ``has_prior_contact`` for rules that want the fact
#: itself rather than its consequence.
NEVER_CONTACTED_HOURS: Final[int] = 8_760

#: ``concession_percent_of_mrr`` when a concession is proposed against a
#: subscription with no recorded price. A concession cannot be justified as a
#: fraction of nothing, so the ratio reads as effectively unbounded and every
#: percentage ceiling trips, rather than a zero that would wave it through.
UNPRICED_CONCESSION_PERCENT: Final[int] = 100_000

#: Actions that put a message in front of the customer, and are therefore
#: subject to quiet hours, consent and frequency caps. ``TRIGGER_STEP_UP`` is
#: excluded on purpose: an AFA challenge is authentication the customer is
#: already waiting on, not outreach, and suppressing it overnight would strand
#: a live payment journey rather than protect anyone from being bothered.
OUTREACH_ACTIONS: Final[frozenset[ActionType]] = frozenset(
    {
        ActionType.REQUEST_INSTRUMENT_UPDATE,
        ActionType.SEND_PAYMENT_LINK,
        ActionType.REQUEST_MANDATE_REAUTH,
        ActionType.SEND_REMINDER,
        ActionType.SEND_DUNNING_NOTICE,
        ActionType.OFFER_PARTIAL_PAYMENT,
        ActionType.OFFER_PLAN_DOWNGRADE,
        ActionType.OFFER_WINBACK_DISCOUNT,
    }
)

#: Actions that consume an attempt against the mandate's retry allowance.
DEBIT_RETRY_ACTIONS: Final[frozenset[ActionType]] = frozenset(
    {ActionType.RETRY_DEBIT, ActionType.SPLIT_DEBIT}
)

#: Failure classes whose retry curve says ``NEVER``. Derived from the taxonomy
#: rather than restated, so adding a terminal class in one place is enough.
TERMINAL_FAILURE_CLASSES: Final[frozenset[FailureClass]] = frozenset(
    fc for fc, curve in RETRY_CURVES.items() if curve.posture is RetryPosture.NEVER
)


def concession_percent_of_mrr(amount_minor: int, mrr_minor: int, *, is_concession: bool) -> int:
    """Concession size as a whole percent of monthly recurring revenue.

    Rounded *up*, deliberately: a ceiling that is applied to a rounded-down
    ratio is a ceiling that can be exceeded by a rupee at a time. Integer
    arithmetic throughout, so this is exact on every machine.
    """
    if not is_concession or amount_minor <= 0:
        return 0
    if mrr_minor <= 0:
        return UNPRICED_CONCESSION_PERCENT
    return min(UNPRICED_CONCESSION_PERCENT, -(-amount_minor * 100 // mrr_minor))


# ---------------------------------------------------------------------------
# The fact catalogue
# ---------------------------------------------------------------------------


class FactKind(StrEnum):
    """The three literal shapes a fact can take. There is no fourth."""

    INT = "int"
    STRING = "string"
    BOOLEAN = "boolean"


@dataclass(frozen=True, slots=True)
class FactSpec:
    """What the expression language is allowed to assume about one fact.

    This is what makes a malformed rule loud rather than quiet. A condition that
    orders a string, compares a boolean to ``1``, or names a failure class that
    does not exist is rejected at validation time -- long before it could have
    silently failed to match and allowed something through.
    """

    name: str
    kind: FactKind
    description: str
    allowed: tuple[str, ...] | None = None
    nullable: bool = False
    minimum: int | None = None
    maximum: int | None = None

    @property
    def is_ordered(self) -> bool:
        """Only integers support ``lt``/``lte``/``gt``/``gte``/``between``."""
        return self.kind is FactKind.INT


def _values(enum_cls: type[StrEnum]) -> tuple[str, ...]:
    return tuple(member.value for member in enum_cls)


def _spec(
    name: str,
    kind: FactKind,
    description: str,
    *,
    allowed: tuple[str, ...] | None = None,
    nullable: bool = False,
    minimum: int | None = None,
    maximum: int | None = None,
) -> tuple[str, FactSpec]:
    return name, FactSpec(
        name=name,
        kind=kind,
        description=description,
        allowed=allowed,
        nullable=nullable,
        minimum=minimum,
        maximum=maximum,
    )


_E = TypeVar("_E", bound=StrEnum)

_INT = FactKind.INT
_STR = FactKind.STRING
_BOOL = FactKind.BOOLEAN

#: The closed catalogue. A condition naming anything not in here is malformed.
FACT_SPECS: Final[Mapping[str, FactSpec]] = dict(
    [
        # --- the proposed action ------------------------------------------
        _spec("action_type", _STR, "Which action is being proposed.", allowed=_values(ActionType)),
        _spec(
            "amount_minor",
            _INT,
            "Money at stake in this action, in minor units. Zero for pure outreach.",
            minimum=0,
        ),
        _spec("currency", _STR, "Currency of every amount in these facts.", allowed=_values(Currency)),
        _spec("is_money_movement", _BOOL, "True when the action debits the customer."),
        _spec("is_concession", _BOOL, "True when the action draws on the concession budget."),
        _spec("is_outreach", _BOOL, "True when the action puts a message in front of the customer."),
        _spec("is_debit_retry", _BOOL, "True when the action consumes a mandate retry allowance."),
        _spec("is_terminal_action", _BOOL, "True when the action closes the case one way or another."),
        # --- the failure ---------------------------------------------------
        _spec(
            "failure_class",
            _STR,
            "Diagnosed failure class, or null before classification.",
            allowed=_values(FailureClass),
            nullable=True,
        ),
        _spec(
            "is_terminal_failure",
            _BOOL,
            "True when the failure class has a NEVER retry posture.",
        ),
        _spec("hours_since_failure", _INT, "Whole hours since the original debit failed.", minimum=0),
        # --- the case ------------------------------------------------------
        _spec("case_attempt_count", _INT, "Recovery attempts already made on this case.", minimum=0),
        _spec(
            "mandate_cycle_attempt_count",
            _INT,
            "Debit attempts already consumed in the mandate's current cycle.",
            minimum=0,
        ),
        _spec("case_contact_count", _INT, "Outreach messages already sent on this case.", minimum=0),
        # --- contact history -------------------------------------------------
        _spec("contacts_last_24h", _INT, "Contacts to this customer in the last 24 hours.", minimum=0),
        _spec("contacts_last_7d", _INT, "Contacts to this customer in the last 7 days.", minimum=0),
        _spec(
            "hours_since_last_contact",
            _INT,
            f"Hours since the last contact; {NEVER_CONTACTED_HOURS} when never contacted.",
            minimum=0,
            maximum=NEVER_CONTACTED_HOURS,
        ),
        _spec("has_prior_contact", _BOOL, "True when this customer has ever been contacted."),
        # --- local time ------------------------------------------------------
        _spec("local_hour_ist", _INT, "Hour of day in IST, 0-23. Quiet hours are an IST concept.",
              minimum=0, maximum=23),
        _spec("local_day_of_month_ist", _INT, "Day of month in IST, 1-31.", minimum=1, maximum=31),
        # --- the customer -----------------------------------------------------
        _spec("customer_tenure_days", _INT, "Days since the customer first subscribed.", minimum=0),
        _spec("lifetime_value_minor", _INT, "Lifetime value booked from this customer.", minimum=0),
        _spec("prior_concession_count", _INT, "Concessions this customer has already had.", minimum=0),
        _spec(
            "prior_concessions_minor",
            _INT,
            "Total value of concessions already granted to this customer.",
            minimum=0,
        ),
        _spec(
            "customer_concession_headroom_minor",
            _INT,
            "What remains of this customer's concession ceiling.",
            minimum=0,
        ),
        _spec(
            "concession_exceeds_customer_ceiling",
            _BOOL,
            "True when this concession would breach the per-customer ceiling.",
        ),
        # --- the money ---------------------------------------------------------
        _spec("subscription_mrr_minor", _INT, "Monthly recurring value of the subscription.",
              minimum=0),
        _spec(
            "concession_percent_of_mrr",
            _INT,
            "This concession as a whole percent of MRR, rounded up.",
            minimum=0,
            maximum=UNPRICED_CONCESSION_PERCENT,
        ),
        _spec(
            "budget_headroom_minor",
            _INT,
            "Unreserved room left in the merchant's concession budget.",
            minimum=0,
        ),
        _spec(
            "concession_exceeds_budget_headroom",
            _BOOL,
            "True when this concession would overdraw the merchant's budget.",
        ),
        # --- consent and authorisation -------------------------------------------
        _spec(
            "purpose",
            _STR,
            "DPDPA purpose this action serves, or null for actions that send nothing.",
            allowed=_values(MessagePurpose),
            nullable=True,
        ),
        _spec(
            "consent_state",
            _STR,
            "Consent state for exactly that purpose, at evaluation time.",
            allowed=_values(ConsentState),
        ),
        _spec(
            "authorisation_decision",
            _STR,
            "What the mandate registry said about this action.",
            allowed=_values(AuthorisationDecision),
        ),
        # --- scores ----------------------------------------------------------------
        _spec("recovery_likelihood", _INT, "Modelled P(recovery), 0-1000.", minimum=0, maximum=1000),
        _spec("churn_risk", _INT, "Modelled P(churn), 0-1000.", minimum=0, maximum=1000),
        # --- the merchant -------------------------------------------------------------
        _spec(
            "merchant_review_first",
            _BOOL,
            "True while the merchant requires a human to see every action.",
        ),
    ]
)

#: Fields whose value is computed from the others. Supplying one is allowed --
#: that is what makes a stored fact row round-trip -- but supplying a value that
#: disagrees with the computation is rejected, so a persisted row can never
#: claim a relation that its own components contradict.
DERIVED_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "is_money_movement",
        "is_concession",
        "is_outreach",
        "is_debit_retry",
        "is_terminal_action",
        "is_terminal_failure",
        "has_prior_contact",
        "concession_percent_of_mrr",
        "concession_exceeds_budget_headroom",
        "concession_exceeds_customer_ceiling",
    }
)


class PolicyFacts(BaseModel):
    """Everything a rule may test, and nothing else.

    Frozen, because a fact set that can change under an evaluator is a fact set
    that cannot be replayed. ``extra="forbid"``, because a typo'd fact name must
    be an error at the boundary rather than a silently ignored key that makes a
    rule never match.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- the proposed action ------------------------------------------------
    action_type: ActionType
    amount_minor: int = Field(default=0, ge=0)
    currency: Currency = Currency.INR
    is_money_movement: bool = False
    is_concession: bool = False
    is_outreach: bool = False
    is_debit_retry: bool = False
    is_terminal_action: bool = False

    # --- the failure ----------------------------------------------------------
    failure_class: FailureClass | None = None
    is_terminal_failure: bool = False
    hours_since_failure: int = Field(default=0, ge=0)

    # --- the case -------------------------------------------------------------
    case_attempt_count: int = Field(default=0, ge=0)
    mandate_cycle_attempt_count: int = Field(default=0, ge=0)
    case_contact_count: int = Field(default=0, ge=0)

    # --- contact history --------------------------------------------------------
    contacts_last_24h: int = Field(default=0, ge=0)
    contacts_last_7d: int = Field(default=0, ge=0)
    hours_since_last_contact: int = Field(default=NEVER_CONTACTED_HOURS, ge=0,
                                          le=NEVER_CONTACTED_HOURS)
    has_prior_contact: bool = False

    # --- local time --------------------------------------------------------------
    local_hour_ist: int = Field(default=12, ge=0, le=23)
    local_day_of_month_ist: int = Field(default=1, ge=1, le=31)

    # --- the customer ---------------------------------------------------------------
    customer_tenure_days: int = Field(default=0, ge=0)
    lifetime_value_minor: int = Field(default=0, ge=0)
    prior_concession_count: int = Field(default=0, ge=0)
    prior_concessions_minor: int = Field(default=0, ge=0)
    customer_concession_headroom_minor: int = Field(default=0, ge=0)
    concession_exceeds_customer_ceiling: bool = False

    # --- the money ---------------------------------------------------------------------
    subscription_mrr_minor: int = Field(default=0, ge=0)
    concession_percent_of_mrr: int = Field(default=0, ge=0, le=UNPRICED_CONCESSION_PERCENT)
    budget_headroom_minor: int = Field(default=0, ge=0)
    concession_exceeds_budget_headroom: bool = False

    # --- consent and authorisation -----------------------------------------------------
    purpose: MessagePurpose | None = None
    consent_state: ConsentState = ConsentState.NEVER_GRANTED
    authorisation_decision: AuthorisationDecision = AuthorisationDecision.DENIED

    # --- scores ---------------------------------------------------------------------------
    recovery_likelihood: int = Field(default=0, ge=0, le=1000)
    churn_risk: int = Field(default=0, ge=0, le=1000)

    # --- the merchant -----------------------------------------------------------------------
    merchant_review_first: bool = True

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, data: Any) -> Any:
        """Compute every derived fact, and refuse a supplied value that disagrees.

        Derivation happens before field validation so that a caller only ever
        has to supply the ground truth. The consistency check is what keeps a
        replayed row honest: a stored fact set whose booleans have been edited
        away from their components will not load.
        """
        if not isinstance(data, Mapping):
            return data
        values: dict[str, Any] = dict(data)
        action = _as_enum(values.get("action_type"), ActionType)
        if action is None:
            return values  # let field validation report the missing or bad action.

        amount = _as_int(values.get("amount_minor"), 0)
        mrr = _as_int(values.get("subscription_mrr_minor"), 0)
        budget = _as_int(values.get("budget_headroom_minor"), 0)
        customer_headroom = _as_int(values.get("customer_concession_headroom_minor"), 0)
        since_contact = _as_int(values.get("hours_since_last_contact"), NEVER_CONTACTED_HOURS)
        if (
            amount is None
            or mrr is None
            or budget is None
            or customer_headroom is None
            or since_contact is None
        ):
            return values  # a component is the wrong type; field validation says so.

        failure_raw = values.get("failure_class")
        failure = None if failure_raw is None else _as_enum(failure_raw, FailureClass)
        if failure_raw is not None and failure is None:
            return values

        concession = action.is_concession
        derived = {
            "is_money_movement": action.moves_money,
            "is_concession": concession,
            "is_outreach": action in OUTREACH_ACTIONS,
            "is_debit_retry": action in DEBIT_RETRY_ACTIONS,
            "is_terminal_action": action.is_terminal,
            "is_terminal_failure": failure in TERMINAL_FAILURE_CLASSES,
            "has_prior_contact": since_contact < NEVER_CONTACTED_HOURS,
            "concession_percent_of_mrr": concession_percent_of_mrr(
                amount, mrr, is_concession=concession
            ),
            "concession_exceeds_budget_headroom": concession and amount > budget,
            "concession_exceeds_customer_ceiling": concession and amount > customer_headroom,
        }
        for key, computed in derived.items():
            supplied = values.get(key)
            if supplied is not None and supplied != computed:
                raise ValueError(
                    f"{key} was given as {supplied!r} but the other facts imply {computed!r}"
                )
            values[key] = computed
        return values

    # ------------------------------------------------------------------ output

    def to_json_dict(self) -> dict[str, Any]:
        """The exact payload persisted in ``PolicyEvaluation.facts``.

        Enums render as their string values and every number is an integer, so
        the row is stable JSON that a later run can load back without loss.
        """
        return self.model_dump(mode="json")

    def field_value(self, name: str) -> Any:
        """Read one fact by name, raising on a name outside the catalogue."""
        if name not in FACT_SPECS:
            raise KeyError(name)
        return self.to_json_dict()[name]


def _as_int(value: Any, default: int) -> int | None:
    """``None`` means "not an integer", which the caller turns into a field error."""
    if value is None:
        return default
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _as_enum(value: Any, enum_cls: type[_E]) -> _E | None:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            return None
    return None


def _assert_catalogue_matches_model() -> None:
    """The catalogue and the model are two statements of one truth; keep them equal.

    Run at import because a drifted catalogue is not a test failure waiting to
    happen, it is a rule that can name a field the validator cannot check.
    """
    modelled = set(PolicyFacts.model_fields)
    catalogued = set(FACT_SPECS)
    if modelled != catalogued:
        missing = sorted(modelled - catalogued)
        extra = sorted(catalogued - modelled)
        raise RuntimeError(
            f"fact catalogue drift: missing specs {missing}, specs without a field {extra}"
        )


_assert_catalogue_matches_model()
