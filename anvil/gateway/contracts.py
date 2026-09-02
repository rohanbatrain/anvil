"""The typed shape of the Razorpay boundary.

Two things live here, and they live together deliberately. The first is the set
of value objects Anvil accepts back from a payment gateway -- orders, payments,
subscriptions, customers, mandates. The second is :class:`RazorpayGateway`, the
Protocol that both the live HTTP client and the offline simulator adapter
satisfy.

Keeping the Protocol out of either implementation is what makes offline mode
honest. If the offline adapter were a special case *inside* the live client,
"it worked in the demo" would tell you nothing about the live path. Because both
are structural implementations of one Protocol, the executor, the reconciler and
the recovery graph run identical code in both modes; only the adapter differs.

Amounts crossing this boundary become :class:`~anvil.domain.money.Money`
immediately. Razorpay speaks minor units natively, so there is no conversion to
get wrong -- but a JSON ``float`` reaching the money path would violate
invariant 3, so the boundary *rejects* one rather than quietly coercing it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from anvil.core.errors import ValidationError
from anvil.domain.money import Currency, Money

#: Razorpay's live API root. Overridable so the sandbox and tests point elsewhere.
DEFAULT_BASE_URL = "https://api.razorpay.com/v1"

#: Razorpay caps ``receipt`` and ``reference_id`` at 40 characters. Anvil uses
#: those fields to carry the idempotency key, so a key that would be silently
#: truncated has to be a hard error rather than a mystery reconciliation miss.
MAX_REFERENCE_LENGTH = 40


# --------------------------------------------------------------------- coercion


def coerce_minor(value: object, *, field_name: str = "amount") -> int:
    """Read a gateway amount as an exact integer count of minor units.

    ``bool`` is rejected because it is an ``int`` subclass and ``True`` would
    otherwise become one paisa. ``float`` is rejected because invariant 3 says
    the money path has no floats in it -- a gateway that started sending
    ``1499.0`` would be a change we must notice loudly, not absorb.
    """
    if isinstance(value, bool):
        raise ValidationError(f"{field_name} must be an integer, got a bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and (value.lstrip("-").isdigit()):
        return int(value)
    raise ValidationError(
        f"{field_name} must be integer minor units, got {type(value).__name__}",
        value=repr(value),
    )


def money_from_gateway(amount: object, currency: object, *, field_name: str = "amount") -> Money:
    """Build :class:`Money` from a gateway amount/currency pair."""
    try:
        code = Currency(str(currency).upper())
    except ValueError as exc:
        raise ValidationError(f"unsupported currency {currency!r}", field=field_name) from exc
    return Money(coerce_minor(amount, field_name=field_name), code)


def epoch_to_utc(value: object, *, field_name: str = "created_at") -> datetime:
    """Razorpay timestamps are epoch seconds; Anvil holds only aware UTC instants."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValidationError(f"{field_name} must be epoch seconds", value=repr(value))
    try:
        seconds = int(value)
    except ValueError as exc:
        raise ValidationError(f"{field_name} must be epoch seconds", value=repr(value)) from exc
    return datetime.fromtimestamp(seconds, tz=UTC)


def optional_epoch(value: object, *, field_name: str = "timestamp") -> datetime | None:
    """As :func:`epoch_to_utc`, but ``None``/absent stays ``None``."""
    return None if value is None else epoch_to_utc(value, field_name=field_name)


def normalise_notes(value: object) -> Mapping[str, str]:
    """Razorpay sends ``notes`` as an object, or as ``[]`` when it is empty."""
    if isinstance(value, Mapping):
        return {str(k): str(v) for k, v in value.items()}
    return {}


def require_reference(value: str, *, field_name: str) -> str:
    """Guard the 40-character ceiling on receipt-style fields."""
    if not value:
        raise ValidationError(f"{field_name} must not be empty")
    if len(value) > MAX_REFERENCE_LENGTH:
        raise ValidationError(
            f"{field_name} exceeds Razorpay's {MAX_REFERENCE_LENGTH}-character limit",
            length=len(value),
        )
    return value


# ---------------------------------------------------------------- value objects


@dataclass(frozen=True, slots=True)
class GatewayOrder:
    """A Razorpay order: the container a debit is attempted against.

    ``receipt`` is load-bearing rather than cosmetic. Anvil writes the
    idempotency key into it, which is what lets the reconciler ask "did the
    order my timed-out request was trying to create actually get created?"
    """

    id: str
    amount: Money
    amount_paid: Money
    amount_due: Money
    status: str
    receipt: str | None
    attempts: int
    created_at: datetime
    notes: Mapping[str, str] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_paid(self) -> bool:
        return self.status == "paid"


@dataclass(frozen=True, slots=True)
class GatewayPayment:
    """One debit attempt as the gateway sees it.

    The four ``error_*`` fields are kept separate rather than flattened because
    they carry different granularity: ``error_code`` is Razorpay's coarse bucket
    (``GATEWAY_ERROR``), ``error_reason`` is the specific cause the decline
    taxonomy can actually classify, and ``error_source``/``error_step`` say who
    refused and at which stage. Collapsing them would throw away the only field
    the classifier wants.
    """

    id: str
    amount: Money
    status: str
    order_id: str | None = None
    invoice_id: str | None = None
    customer_id: str | None = None
    token_id: str | None = None
    method: str | None = None
    captured: bool = False
    error_code: str | None = None
    error_description: str | None = None
    error_source: str | None = None
    error_step: str | None = None
    error_reason: str | None = None
    created_at: datetime | None = None
    notes: Mapping[str, str] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_settled(self) -> bool:
        """Money has definitively moved. ``authorized`` has not settled yet."""
        return self.status == "captured"

    @property
    def is_failed(self) -> bool:
        return self.status == "failed"

    @property
    def is_terminal(self) -> bool:
        """The gateway will not change its mind about this payment."""
        return self.status in ("captured", "failed", "refunded")

    @property
    def best_failure_code(self) -> str | None:
        """The most specific decline code available, for the taxonomy lookup.

        ``error_reason`` first because ``error_code`` is a bucket -- classifying
        on ``GATEWAY_ERROR`` would collapse an issuer outage and a revoked
        mandate into one posture, which is exactly the mistake Anvil exists to
        avoid.
        """
        return self.error_reason or self.error_code


@dataclass(frozen=True, slots=True)
class GatewayPaymentLink:
    """A hosted page the customer can pay on when the mandate cannot be debited."""

    id: str
    short_url: str
    status: str
    amount: Money
    reference_id: str | None = None
    expire_by: datetime | None = None
    created_at: datetime | None = None
    notes: Mapping[str, str] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GatewaySubscription:
    """The recurring agreement a recovery case hangs off."""

    id: str
    plan_id: str | None
    customer_id: str | None
    status: str
    total_count: int = 0
    paid_count: int = 0
    remaining_count: int = 0
    current_start: datetime | None = None
    current_end: datetime | None = None
    charge_at: datetime | None = None
    ended_at: datetime | None = None
    short_url: str | None = None
    notes: Mapping[str, str] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_recoverable(self) -> bool:
        """``halted`` and ``pending`` are the states a recovery case works on."""
        return self.status in ("pending", "halted", "active")


@dataclass(frozen=True, slots=True)
class GatewayCustomer:
    """The payer. Contact fields are carried but never logged or audited."""

    id: str
    name: str | None = None
    email: str | None = None
    contact: str | None = None
    created_at: datetime | None = None
    notes: Mapping[str, str] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GatewayMandate:
    """A stored debit authority -- a UPI Autopay mandate, e-NACH UMRN or card token.

    Razorpay models all three as a ``token``, which is convenient: one fetch
    answers "may we still debit this customer?" for every rail. Anvil mirrors
    the answer into its own authorisation registry rather than trusting this
    object at decision time, because authorisation must be provable locally.
    """

    id: str
    customer_id: str | None
    method: str | None
    recurring_status: str | None
    max_amount: Money | None = None
    mandate_reference: str | None = None
    bank: str | None = None
    failure_reason: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    expired_at: datetime | None = None
    created_at: datetime | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_debitable(self) -> bool:
        """Only a confirmed mandate may back a debit. Anything else fails closed."""
        return self.recurring_status == "confirmed"


# -------------------------------------------------------------------- protocol


@runtime_checkable
class RazorpayGateway(Protocol):
    """Everything Anvil is allowed to ask a payment gateway to do.

    The surface is deliberately small. Every mutating method takes a
    caller-supplied ``idempotency_key`` -- invariant 5 -- and no method exposes a
    way to move money without one. A caller cannot accidentally retry a debit
    unsafely, because there is no signature that permits it.
    """

    async def create_order(
        self,
        *,
        amount: Money,
        idempotency_key: str,
        notes: Mapping[str, str] | None = None,
    ) -> GatewayOrder:
        """Create the order a debit will be attempted against.

        ``idempotency_key`` is also written to ``receipt``, giving the reconciler
        a server-side handle on an order whose creation response was lost.
        """

    async def fetch_order(self, order_id: str) -> GatewayOrder:
        """Read one order by its Razorpay id."""

    async def fetch_order_by_receipt(self, receipt: str) -> GatewayOrder | None:
        """Find the order carrying this receipt, or ``None`` if it was never created.

        This is the reconciler's primary question after a timeout: absence here
        is strong evidence that the request never landed and no money moved.
        """

    async def fetch_payments_for_order(self, order_id: str) -> tuple[GatewayPayment, ...]:
        """Every payment attempted against an order, newest state included."""

    async def fetch_payment(self, payment_id: str) -> GatewayPayment:
        """Read one payment by its Razorpay id."""

    async def create_payment_link(
        self,
        *,
        amount: Money,
        description: str,
        idempotency_key: str,
        customer_name: str | None = None,
        customer_email: str | None = None,
        customer_contact: str | None = None,
        expire_by: datetime | None = None,
        notes: Mapping[str, str] | None = None,
    ) -> GatewayPaymentLink:
        """Create a hosted payment page; the key goes in ``reference_id``."""

    async def fetch_subscription(self, subscription_id: str) -> GatewaySubscription:
        """Read the current state of a recurring agreement."""

    async def charge_subscription_invoice(
        self,
        *,
        order_id: str,
        customer_id: str,
        token_id: str,
        amount: Money,
        idempotency_key: str,
        email: str,
        contact: str,
        description: str | None = None,
    ) -> GatewayPayment:
        """Debit a registered mandate for a subscription's outstanding invoice.

        ``email`` and ``contact`` are required by the recurring-charge API. They
        pass straight through to Razorpay and are never logged, audited or sent
        to a model.
        """

    async def create_customer(
        self,
        *,
        name: str,
        email: str,
        contact: str,
        idempotency_key: str,
        notes: Mapping[str, str] | None = None,
    ) -> GatewayCustomer:
        """Create -- or return the existing -- customer record."""

    async def fetch_customer(self, customer_id: str) -> GatewayCustomer:
        """Read one customer by its Razorpay id."""

    async def fetch_mandate(self, *, customer_id: str, token_id: str) -> GatewayMandate:
        """Read the mandate/token backing recurring debits for this customer."""

    async def aclose(self) -> None:
        """Release transport resources. Safe to call more than once."""
