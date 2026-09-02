"""Razorpay webhook payloads, parsed into one normalised internal event.

Razorpay's webhook body is a small envelope wrapped around a variable set of
entities: ``payment.failed`` carries a payment, ``subscription.charged`` carries
both a subscription and a payment, ``order.paid`` carries an order and a
payment. Every downstream consumer wants the same five facts out of that --
which entity moved, which ids it touches, how much money is involved, what went
wrong, and when -- so the parsing happens exactly once, here, and the rest of
Anvil never sees a raw webhook dict.

Two rules shape this module.

**Unknown event types are accepted, never fatal.** Razorpay adds events; a
merchant enables one we have not modelled. An endpoint that 500s on an
unrecognised ``event`` field teaches Razorpay's delivery system to retry
forever and, worse, hides the real events behind a wall of failures. An
unrecognised type is parsed on a best-effort basis, marked
``recognised=False``, recorded, and acknowledged.

**Extraction is defensive by construction.** Different rails put the same fact
in different places -- a UPI Autopay mandate reference may arrive as ``umn``,
``mrn`` or nested under ``recurring_details`` depending on the flow. Probing a
short list of known locations is honest about that heterogeneity; asserting one
canonical path and crashing on the others is not.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

import orjson

from anvil.core.errors import ValidationError
from anvil.domain.money import Money
from anvil.gateway.contracts import epoch_to_utc, money_from_gateway, normalise_notes

#: Razorpay's signature header, lower-cased. Header lookup here is always
#: case-insensitive: ASGI servers normalise, but a test or a proxy may not.
SIGNATURE_HEADER: Final = "x-razorpay-signature"
EVENT_ID_HEADER: Final = "x-razorpay-event-id"
ACCOUNT_ID_HEADER: Final = "x-razorpay-account-id"


class WebhookEntity(StrEnum):
    """The Razorpay entity an event is *about*.

    This is Razorpay's wire vocabulary, not Anvil's domain vocabulary -- it names
    the aggregate a webhook advances so the ordering guard knows what cursor to
    compare against. Anvil's own closed vocabulary stays in ``anvil.domain.enums``.
    """

    PAYMENT = "payment"
    ORDER = "order"
    SUBSCRIPTION = "subscription"
    INVOICE = "invoice"
    REFUND = "refund"
    TOKEN = "token"
    UNKNOWN = "unknown"


#: Lifecycles that provably cannot run backwards, and the rank of each state.
#:
#: Only three entities appear here, and the omissions are the interesting part.
#: A subscription legitimately oscillates -- ``pending`` becomes ``active`` again
#: the moment a recovery attempt settles -- and a token moves ``paused`` ->
#: ``confirmed`` when a customer resumes it. Ranking those states would make the
#: ordering guard discard exactly the events Anvil most needs. For them, the
#: event timestamp is the only monotonic quantity we are entitled to trust.
MONOTONIC_STATE_RANKS: Final[dict[WebhookEntity, dict[str, int]]] = {
    WebhookEntity.PAYMENT: {
        "created": 0,
        "authorized": 1,
        "failed": 2,
        "captured": 3,
        "refunded": 4,
    },
    WebhookEntity.ORDER: {"created": 0, "attempted": 1, "paid": 2},
    WebhookEntity.REFUND: {"created": 0, "pending": 1, "failed": 2, "processed": 2},
}

#: Payload keys that carry personal data. They stay out of every derived
#: structure Anvil persists; invariant 10 is enforced on the way in, not on read.
PII_KEYS: Final[frozenset[str]] = frozenset(
    {"email", "contact", "vpa", "customer_email", "customer_contact", "card", "bank_details"}
)


@dataclass(frozen=True, slots=True)
class WebhookEnvelope:
    """The verified outer shell of a delivery, plus the bytes it arrived as.

    ``raw_body`` is retained because it, not the parsed dict, is what the HMAC
    was computed over. Anything that re-derives bytes from ``body`` is wrong by
    construction -- see :mod:`anvil.gateway.webhooks`.
    """

    event_id: str
    event_type: str
    created_at: datetime
    raw_body: bytes
    body: Mapping[str, Any]
    signature: str = ""
    account_id: str | None = None
    contains: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NormalisedEvent:
    """One webhook, reduced to the facts Anvil acts on.

    Every id field is optional because different events carry different subsets;
    a consumer that needs one asserts it rather than the parser inventing it.
    ``raw`` is kept for debugging and replay, and is the one field that must
    never reach the audit log -- use :meth:`audit_payload` for that.
    """

    event_id: str
    event_type: str
    entity: WebhookEntity
    aggregate_id: str
    occurred_at: datetime
    recognised: bool = True
    account_id: str | None = None
    entity_state: str | None = None
    amount: Money | None = None

    payment_id: str | None = None
    order_id: str | None = None
    subscription_id: str | None = None
    invoice_id: str | None = None
    customer_id: str | None = None
    token_id: str | None = None
    refund_id: str | None = None
    mandate_reference: str | None = None
    method: str | None = None

    failure_code: str | None = None
    failure_description: str | None = None
    failure_source: str | None = None
    failure_step: str | None = None
    failure_reason: str | None = None

    notes: Mapping[str, str] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_failure(self) -> bool:
        """True when this event reports a debit that did not go through."""
        return self.event_type in _FAILURE_EVENTS or self.entity_state == "failed"

    @property
    def amount_minor(self) -> int | None:
        return None if self.amount is None else self.amount.minor

    def audit_payload(self) -> dict[str, Any]:
        """A persistence-safe projection: ids, money and codes, no personal data.

        Redaction happens here rather than at the audit writer because the parser
        is the only place that knows which of Razorpay's many fields are
        contact details. A caller cannot forget to redact if the only structure
        it is handed is already redacted.
        """
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "entity": self.entity.value,
            "aggregate_id": self.aggregate_id,
            "recognised": self.recognised,
            "occurred_at": self.occurred_at.isoformat(),
            "entity_state": self.entity_state,
            "amount_minor": self.amount_minor,
            "currency": None if self.amount is None else self.amount.currency.value,
            "payment_id": self.payment_id,
            "order_id": self.order_id,
            "subscription_id": self.subscription_id,
            "invoice_id": self.invoice_id,
            "customer_id": self.customer_id,
            "token_id": self.token_id,
            "refund_id": self.refund_id,
            "mandate_reference": self.mandate_reference,
            "method": self.method,
            "failure_code": self.failure_code,
            "failure_description": self.failure_description,
            "failure_source": self.failure_source,
            "failure_step": self.failure_step,
        }


# --------------------------------------------------------------- envelope parse


def header_value(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup that works on a plain ``dict`` too."""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def parse_envelope(raw_body: bytes, headers: Mapping[str, str]) -> WebhookEnvelope:
    """Decode the outer envelope. Raises :class:`ValidationError` on anything malformed.

    Called *after* signature verification, never before: parsing attacker-supplied
    JSON that has not been authenticated is work done on behalf of an attacker.
    """
    try:
        decoded: Any = orjson.loads(raw_body)
    except orjson.JSONDecodeError as exc:
        raise ValidationError("webhook body is not valid JSON") from exc
    if not isinstance(decoded, Mapping):
        raise ValidationError("webhook body must be a JSON object")

    event_type = decoded.get("event")
    if not isinstance(event_type, str) or not event_type:
        raise ValidationError("webhook body has no 'event' field")

    event_id = header_value(headers, EVENT_ID_HEADER)
    if not event_id:
        raise ValidationError(
            f"missing {EVENT_ID_HEADER}; without it the delivery cannot be deduplicated"
        )

    created_at = epoch_to_utc(decoded.get("created_at"), field_name="created_at")
    contains = decoded.get("contains")
    return WebhookEnvelope(
        event_id=event_id,
        event_type=event_type,
        created_at=created_at,
        raw_body=raw_body,
        body=decoded,
        signature=header_value(headers, SIGNATURE_HEADER) or "",
        account_id=header_value(headers, ACCOUNT_ID_HEADER)
        or _as_str(decoded.get("account_id")),
        contains=tuple(c for c in contains if isinstance(c, str))
        if isinstance(contains, list)
        else (),
    )


# ------------------------------------------------------------------- extraction


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _entity(envelope: WebhookEnvelope, name: str) -> Mapping[str, Any]:
    """Pull ``payload.<name>.entity``, returning an empty mapping when absent."""
    payload = envelope.body.get("payload")
    if not isinstance(payload, Mapping):
        return {}
    section = payload.get(name)
    if not isinstance(section, Mapping):
        return {}
    entity = section.get("entity")
    return entity if isinstance(entity, Mapping) else {}


def _nested(mapping: Mapping[str, Any], *path: str) -> Any:
    cursor: Any = mapping
    for step in path:
        if not isinstance(cursor, Mapping):
            return None
        cursor = cursor.get(step)
    return cursor


def _first_str(*candidates: object) -> str | None:
    for candidate in candidates:
        found = _as_str(candidate)
        if found is not None:
            return found
    return None


def _amount_of(entity: Mapping[str, Any]) -> Money | None:
    if "amount" not in entity or entity.get("amount") is None:
        return None
    return money_from_gateway(entity["amount"], entity.get("currency", "INR"))


@dataclass(frozen=True, slots=True)
class _Entities:
    """Every entity a delivery might carry, resolved once per event."""

    payment: Mapping[str, Any]
    order: Mapping[str, Any]
    subscription: Mapping[str, Any]
    invoice: Mapping[str, Any]
    refund: Mapping[str, Any]
    token: Mapping[str, Any]
    customer: Mapping[str, Any]
    payment_link: Mapping[str, Any]


def _entities(envelope: WebhookEnvelope) -> _Entities:
    return _Entities(
        payment=_entity(envelope, "payment"),
        order=_entity(envelope, "order"),
        subscription=_entity(envelope, "subscription"),
        invoice=_entity(envelope, "invoice"),
        refund=_entity(envelope, "refund"),
        token=_entity(envelope, "token"),
        customer=_entity(envelope, "customer"),
        payment_link=_entity(envelope, "payment_link"),
    )


def _mandate_reference(ents: _Entities) -> str | None:
    """Find the mandate identifier across the shapes the rails actually use.

    UPI Autopay calls it a UMN, e-NACH a UMRN, and Razorpay surfaces it as
    ``mrn`` on some token payloads and nested under ``recurring_details`` or
    ``upi`` on others. All four are the same fact.
    """
    token = ents.token
    return _first_str(
        token.get("umn"),
        token.get("mrn"),
        _nested(token, "upi", "umn"),
        _nested(token, "recurring_details", "umn"),
        _nested(token, "bank_details", "umrn"),
        _nested(ents.payment, "acquirer_data", "umn"),
    )


def _failure_of(ents: _Entities) -> tuple[str | None, ...]:
    """``(code, description, source, step, reason)`` from wherever they live."""
    payment = ents.payment
    reason = _first_str(
        payment.get("error_reason"),
        _nested(ents.token, "recurring_details", "failure_reason"),
    )
    code = _first_str(payment.get("error_code"))
    description = _first_str(payment.get("error_description"))
    return (
        reason or code,
        description,
        _first_str(payment.get("error_source")),
        _first_str(payment.get("error_step")),
        reason,
    )


def _normalise(
    envelope: WebhookEnvelope,
    ents: _Entities,
    *,
    entity: WebhookEntity,
    aggregate_id: str | None,
    entity_state: str | None,
    amount: Money | None,
    recognised: bool = True,
) -> NormalisedEvent:
    """Assemble the normalised event once the parser has chosen an aggregate."""
    code, description, source, step, reason = _failure_of(ents)
    notes_source = (
        ents.payment
        or ents.subscription
        or ents.order
        or ents.payment_link
        or ents.refund
        or ents.token
    )
    return NormalisedEvent(
        event_id=envelope.event_id,
        event_type=envelope.event_type,
        entity=entity,
        # An event whose aggregate id is missing is still worth recording; the
        # event id is a stable stand-in so the row is never keyless.
        aggregate_id=aggregate_id or envelope.event_id,
        occurred_at=envelope.created_at,
        recognised=recognised,
        account_id=envelope.account_id,
        entity_state=entity_state,
        amount=amount,
        payment_id=_first_str(ents.payment.get("id")),
        order_id=_first_str(ents.order.get("id"), ents.payment.get("order_id")),
        subscription_id=_first_str(
            ents.subscription.get("id"),
            ents.payment.get("subscription_id"),
            ents.invoice.get("subscription_id"),
        ),
        invoice_id=_first_str(ents.invoice.get("id"), ents.payment.get("invoice_id")),
        customer_id=_first_str(
            ents.customer.get("id"),
            ents.payment.get("customer_id"),
            ents.subscription.get("customer_id"),
            ents.token.get("customer_id"),
            ents.invoice.get("customer_id"),
        ),
        token_id=_first_str(ents.token.get("id"), ents.payment.get("token_id")),
        refund_id=_first_str(ents.refund.get("id")),
        mandate_reference=_mandate_reference(ents),
        method=_first_str(ents.payment.get("method"), ents.token.get("method")),
        failure_code=code,
        failure_description=description,
        failure_source=source,
        failure_step=step,
        failure_reason=reason,
        notes=normalise_notes(notes_source.get("notes")),
        raw=envelope.body,
    )


# ---------------------------------------------------------------------- parsers


def parse_payment_event(envelope: WebhookEnvelope) -> NormalisedEvent:
    """``payment.authorized`` / ``payment.captured`` / ``payment.failed``."""
    ents = _entities(envelope)
    return _normalise(
        envelope,
        ents,
        entity=WebhookEntity.PAYMENT,
        aggregate_id=_as_str(ents.payment.get("id")),
        entity_state=_as_str(ents.payment.get("status")),
        amount=_amount_of(ents.payment),
    )


def parse_order_event(envelope: WebhookEnvelope) -> NormalisedEvent:
    """``order.paid``. The order is the aggregate; the payment rides along.

    The amount reported is the order's, not the payment's, because a partially
    paid order is a different fact from the payment that partially paid it.
    """
    ents = _entities(envelope)
    return _normalise(
        envelope,
        ents,
        entity=WebhookEntity.ORDER,
        aggregate_id=_as_str(ents.order.get("id")),
        entity_state=_as_str(ents.order.get("status")),
        amount=_amount_of(ents.order),
    )


def parse_subscription_event(envelope: WebhookEnvelope) -> NormalisedEvent:
    """Every ``subscription.*`` event.

    ``subscription.charged`` carries a payment as well; when it does, the money
    reported is the payment's, since that is what actually settled. Otherwise
    there is no amount -- a halted subscription has not moved any money, and
    inventing a figure from the plan would put a number in the ledger's line of
    sight that nobody debited.
    """
    ents = _entities(envelope)
    return _normalise(
        envelope,
        ents,
        entity=WebhookEntity.SUBSCRIPTION,
        aggregate_id=_as_str(ents.subscription.get("id")),
        entity_state=_as_str(ents.subscription.get("status")),
        amount=_amount_of(ents.payment),
    )


def parse_refund_event(envelope: WebhookEnvelope) -> NormalisedEvent:
    """``refund.processed`` and its siblings."""
    ents = _entities(envelope)
    return _normalise(
        envelope,
        ents,
        entity=WebhookEntity.REFUND,
        aggregate_id=_as_str(ents.refund.get("id")),
        entity_state=_as_str(ents.refund.get("status")),
        amount=_amount_of(ents.refund),
    )


def parse_token_event(envelope: WebhookEnvelope) -> NormalisedEvent:
    """``token.*`` -- the mandate lifecycle for UPI Autopay, e-NACH and cards.

    The state reported is ``recurring_details.status``, not the token's own
    ``status``: a token can be alive while its recurring authority has been
    rejected or cancelled, and it is the recurring authority Anvil needs.
    """
    ents = _entities(envelope)
    state = _first_str(
        _nested(ents.token, "recurring_details", "status"), ents.token.get("status")
    )
    max_amount = ents.token.get("max_amount")
    return _normalise(
        envelope,
        ents,
        entity=WebhookEntity.TOKEN,
        aggregate_id=_as_str(ents.token.get("id")),
        entity_state=state,
        amount=None
        if max_amount is None
        else money_from_gateway(max_amount, ents.token.get("currency", "INR")),
    )


def parse_invoice_event(envelope: WebhookEnvelope) -> NormalisedEvent:
    """``invoice.*`` -- the billing document a subscription charge is raised against."""
    ents = _entities(envelope)
    return _normalise(
        envelope,
        ents,
        entity=WebhookEntity.INVOICE,
        aggregate_id=_as_str(ents.invoice.get("id")),
        entity_state=_as_str(ents.invoice.get("status")),
        amount=_amount_of(ents.invoice),
    )


def parse_unknown_event(envelope: WebhookEnvelope) -> NormalisedEvent:
    """Best-effort parse of an event type Anvil does not model.

    It still gets an aggregate, an amount where one is obvious, and a row in
    ``processed_webhooks``. What it does not get is a business reaction: the
    graph never sees it. Recording an unmodelled event costs one insert and buys
    the ability to answer "did Razorpay tell us about this?" six months later.
    """
    ents = _entities(envelope)
    for entity_kind, mapping in (
        (WebhookEntity.PAYMENT, ents.payment),
        (WebhookEntity.SUBSCRIPTION, ents.subscription),
        (WebhookEntity.ORDER, ents.order),
        (WebhookEntity.INVOICE, ents.invoice),
        (WebhookEntity.REFUND, ents.refund),
        (WebhookEntity.TOKEN, ents.token),
    ):
        found = _as_str(mapping.get("id"))
        if found is not None:
            return _normalise(
                envelope,
                ents,
                entity=entity_kind,
                aggregate_id=found,
                entity_state=_as_str(mapping.get("status")),
                amount=_amount_of(mapping),
                recognised=False,
            )
    return _normalise(
        envelope,
        ents,
        entity=WebhookEntity.UNKNOWN,
        aggregate_id=None,
        entity_state=None,
        amount=None,
        recognised=False,
    )


Parser = Callable[[WebhookEnvelope], NormalisedEvent]

#: Event type -> parser. Membership in this table is what "Anvil cares about"
#: means; everything else routes to :func:`parse_unknown_event`.
PARSERS: Final[dict[str, Parser]] = {
    "payment.authorized": parse_payment_event,
    "payment.captured": parse_payment_event,
    "payment.failed": parse_payment_event,
    "order.paid": parse_order_event,
    "refund.processed": parse_refund_event,
    "refund.failed": parse_refund_event,
    "subscription.authenticated": parse_subscription_event,
    "subscription.activated": parse_subscription_event,
    "subscription.charged": parse_subscription_event,
    "subscription.pending": parse_subscription_event,
    "subscription.halted": parse_subscription_event,
    "subscription.cancelled": parse_subscription_event,
    "subscription.paused": parse_subscription_event,
    "subscription.resumed": parse_subscription_event,
    "subscription.completed": parse_subscription_event,
    "invoice.paid": parse_invoice_event,
    "invoice.expired": parse_invoice_event,
    "token.confirmed": parse_token_event,
    "token.rejected": parse_token_event,
    "token.paused": parse_token_event,
    "token.cancelled": parse_token_event,
    "token.expired": parse_token_event,
}

#: Event types that report a debit which did not go through.
_FAILURE_EVENTS: Final[frozenset[str]] = frozenset(
    {"payment.failed", "subscription.pending", "subscription.halted", "token.rejected"}
)

#: Sorted for stable rendering in docs and the console's webhook inspector.
KNOWN_EVENT_TYPES: Final[tuple[str, ...]] = tuple(sorted(PARSERS))


def parse_event(envelope: WebhookEnvelope) -> NormalisedEvent:
    """Normalise a verified delivery. Total: every input produces an event."""
    return PARSERS.get(envelope.event_type, parse_unknown_event)(envelope)


def is_recognised(event_type: str) -> bool:
    return event_type in PARSERS
