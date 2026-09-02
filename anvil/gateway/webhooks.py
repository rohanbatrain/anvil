"""Webhook verification: signature, replay window, dedupe, ordering.

This is where invariant 4 lives -- *every inbound webhook is processed at most
once* -- and it is deliberately built as four independent, separately testable
steps rather than one method. Each step has a distinct failure mode and a
distinct correct response, and fusing them makes it impossible to prove any one
of them in isolation:

1. :func:`verify_signature` -- is this actually from Razorpay?  -> 400
2. :func:`check_replay_window` -- is it a captured delivery being replayed? -> 400
3. :func:`claim_event` -- have we already processed it? -> 200, nothing re-run
4. :func:`check_ordering` -- is it older than the state we hold? -> recorded, discarded

The order is not arbitrary. Verification happens over the raw bytes before the
body is parsed, so unauthenticated JSON is never handed to a decoder. Dedupe
happens before any business logic, so a duplicate cannot half-execute.

**Why the raw bytes.** ``verify_signature`` takes ``bytes``, not a dict, and the
distinction is the whole ballgame. Razorpay computes HMAC-SHA256 over the exact
byte sequence it transmitted. Parsing that JSON and re-serialising it produces a
*different* byte sequence -- key order, whitespace, unicode escaping and float
formatting are all serialiser choices, none of which round-trip -- so a
signature checked against a re-serialised body is guaranteed to mismatch, and
the only way to make the endpoint "work" after that mistake is to stop checking
the signature. Frameworks make this easy to get wrong because the parsed body is
the convenient thing to reach for. Anvil therefore never accepts a parsed body
here; the type signature refuses it.

**Why the constraint, not a SELECT.** Dedupe is done by *attempting the insert*
and catching Postgres 23505. A ``SELECT`` before the ``INSERT`` is a race: two
concurrent deliveries of the same event both see no row, both proceed, and both
run the business logic. The unique index on ``processed_webhooks.event_id`` is
the only thing that is actually atomic, so it is the mechanism -- the exception
is not an error path, it is the answer.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from anvil.core.clock import Clock
from anvil.core.errors import (
    DuplicateEvent,
    StaleEvent,
    WebhookReplayRejected,
    WebhookVerificationFailed,
)
from anvil.core.ids import IdPrefix, new_id
from anvil.db.models.platform import ProcessedWebhook
from anvil.gateway.events import (
    MONOTONIC_STATE_RANKS,
    SIGNATURE_HEADER,
    NormalisedEvent,
    WebhookEntity,
    WebhookEnvelope,
    header_value,
    parse_envelope,
    parse_event,
)

#: Postgres SQLSTATE for a unique-violation. The only code that means "duplicate".
UNIQUE_VIOLATION: Final = "23505"

#: Default replay tolerance. Matches ``Settings.webhook_tolerance_seconds``; the
#: caller normally passes the configured value rather than relying on this.
DEFAULT_TOLERANCE_SECONDS: Final = 300


# ------------------------------------------------------------- 1. the signature


def expected_signature(raw_body: bytes, secret: str) -> str:
    """The hex HMAC-SHA256 Razorpay would have sent for exactly these bytes."""
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """Constant-time check of ``X-Razorpay-Signature`` against the raw body.

    ``raw_body`` must be the bytes as received. See the module docstring: a
    re-serialised body cannot match, and making it match would mean weakening
    the check.

    Comparison uses :func:`hmac.compare_digest` so the runtime does not leak how
    many leading characters of a forged signature were correct. An empty secret
    or an empty header returns ``False`` -- a misconfigured deployment must
    accept nothing, not everything.
    """
    if not secret or not signature_header:
        return False
    return hmac.compare_digest(expected_signature(raw_body, secret), signature_header)


def require_signature(raw_body: bytes, signature_header: str, secret: str) -> None:
    """As :func:`verify_signature`, raising the API's 400 instead of returning False."""
    if not verify_signature(raw_body, signature_header, secret):
        raise WebhookVerificationFailed(
            "webhook signature does not match the raw request body",
            body_bytes=len(raw_body),
        )


def body_digest(raw_body: bytes) -> str:
    """SHA-256 of the delivered bytes, stored alongside the dedupe row.

    A replay of a known event id with a *mutated* body would otherwise be
    invisible: the insert would collide and we would answer 200 having never
    looked at what changed. Keeping the digest makes that detectable after the
    fact.
    """
    return hashlib.sha256(raw_body).hexdigest()


# ---------------------------------------------------------- 2. the replay window


def check_replay_window(
    payload_timestamp: datetime,
    now: datetime,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> None:
    """Reject a delivery whose own timestamp is too far from now.

    A valid signature proves authorship, not freshness: a captured delivery
    replayed a week later still verifies. The payload timestamp is inside the
    signed bytes, so an attacker cannot move it without invalidating the
    signature -- which makes it a usable freshness bound.

    The window is symmetric. Skew in either direction is a clock problem or an
    attack, and both deserve a 400 rather than a guess: silently accepting
    future-dated events would let one arrive "before" the state it describes and
    poison the ordering guard.
    """
    if payload_timestamp.tzinfo is None or now.tzinfo is None:
        raise ValueError("replay window needs timezone-aware instants")
    if tolerance_seconds < 0:
        raise ValueError("tolerance_seconds must not be negative")
    skew = abs((now - payload_timestamp).total_seconds())
    if skew > tolerance_seconds:
        raise WebhookReplayRejected(
            "webhook timestamp is outside the replay tolerance window",
            skew_seconds=int(skew),
            tolerance_seconds=tolerance_seconds,
        )


# ------------------------------------------------------------------ 3. the dedupe


def is_unique_violation(error: IntegrityError) -> bool:
    """True when an IntegrityError is specifically SQLSTATE 23505.

    The driver is not fixed -- asyncpg exposes ``sqlstate``, psycopg exposes
    ``sqlstate``, psycopg2 exposes ``pgcode`` -- so all three are checked before
    falling back to the message. Narrowing to 23505 matters: a foreign-key or
    check violation is a bug, and answering 200 to one would hide it.
    """
    orig: Any = error.orig
    for attribute in ("sqlstate", "pgcode"):
        code = getattr(orig, attribute, None)
        if isinstance(code, str) and code:
            return code == UNIQUE_VIOLATION
    return UNIQUE_VIOLATION in str(orig)


def build_webhook_record(
    envelope: WebhookEnvelope, event: NormalisedEvent, *, received_at: datetime
) -> ProcessedWebhook:
    """The dedupe row for one delivery, unsaved."""
    return ProcessedWebhook(
        id=new_id(IdPrefix.WEBHOOK),
        event_id=envelope.event_id,
        event_type=envelope.event_type,
        event_timestamp=envelope.created_at,
        received_at=received_at,
        body_digest=body_digest(envelope.raw_body),
        entity_id=event.aggregate_id,
    )


async def claim_event(
    session: AsyncSession,
    envelope: WebhookEnvelope,
    event: NormalisedEvent,
    *,
    clock: Clock,
) -> ProcessedWebhook:
    """Take exclusive ownership of this event id, or raise :class:`DuplicateEvent`.

    The insert runs inside a SAVEPOINT. Postgres aborts the whole transaction on
    a constraint violation, so without one the caller would be left holding a
    dead transaction and could not even record that a duplicate arrived. The
    savepoint scopes the damage to the failed insert and leaves the surrounding
    transaction usable.

    ``DuplicateEvent`` carries ``http_status = 200``: a duplicate is a correct,
    expected outcome of at-least-once delivery, not an error. The API answers
    200 and runs no business logic -- which is invariant 4 stated operationally.
    """
    record = build_webhook_record(envelope, event, received_at=clock.now())
    try:
        async with session.begin_nested():
            session.add(record)
            await session.flush()
    except IntegrityError as exc:
        if not is_unique_violation(exc):
            raise
        raise DuplicateEvent(
            "webhook already processed",
            event_id=envelope.event_id,
            event_type=envelope.event_type,
        ) from exc
    return record


def mark_processed(record: ProcessedWebhook, *, now: datetime) -> ProcessedWebhook:
    """Stamp a delivery as fully handled. Called after business logic commits."""
    record.processed_at = now
    return record


def mark_discarded(record: ProcessedWebhook, reason: str, *, now: datetime) -> ProcessedWebhook:
    """Record *why* a delivery was accepted but not acted on.

    Stale and unrecognised events land here. The row is the evidence that the
    event arrived and was consciously dropped, which is a different claim from
    "we never received it" -- and the only one of the two that a support
    conversation can be built on.
    """
    record.processed_at = now
    record.processing_error = reason
    return record


# ---------------------------------------------------------------- 4. the ordering


class OrderingVerdict(StrEnum):
    """What to do with an event relative to the state already held."""

    APPLY = "apply"
    DISCARD_STALE = "discard_stale"


@dataclass(frozen=True, slots=True)
class AggregateCursor:
    """What Anvil currently believes about one gateway aggregate.

    ``version`` is *ours*, not Razorpay's -- Razorpay does not version its
    entities. It is the per-aggregate counter on ``domain_events``, and the
    unique constraint ``(aggregate_type, aggregate_id, aggregate_version)`` is
    the backstop that makes a concurrent double-apply impossible even if this
    guard were bypassed. The guard is the cheap check; the constraint is the
    guarantee.
    """

    aggregate_type: WebhookEntity
    aggregate_id: str
    version: int
    last_event_at: datetime
    last_state: str | None = None

    @property
    def next_version(self) -> int:
        return self.version + 1


@dataclass(frozen=True, slots=True)
class OrderingDecision:
    """The guard's answer, with the reason it reached it."""

    verdict: OrderingVerdict
    reason: str
    next_version: int

    @property
    def should_apply(self) -> bool:
        return self.verdict is OrderingVerdict.APPLY


def state_rank(entity: WebhookEntity, state: str | None) -> int | None:
    """Position of ``state`` in a provably monotonic lifecycle, else ``None``.

    ``None`` means "this entity's states are not ordered" and is not a failure --
    see :data:`~anvil.gateway.events.MONOTONIC_STATE_RANKS` for why subscriptions
    and tokens are deliberately absent.
    """
    if state is None:
        return None
    return MONOTONIC_STATE_RANKS.get(entity, {}).get(state)


def check_ordering(event: NormalisedEvent, cursor: AggregateCursor | None) -> OrderingDecision:
    """Decide whether an event advances the aggregate or arrived too late.

    Two independent signals, applied in order of strength:

    * **Lifecycle rank.** Where the entity's states cannot run backwards, a
      lower-ranked state is stale no matter what its timestamp says -- an
      ``authorized`` event delivered after ``captured`` describes a moment we
      have already moved past.
    * **Timestamp.** Otherwise, an event stamped strictly earlier than the last
      one applied is stale. Equal timestamps are allowed through: Razorpay's
      timestamps have one-second resolution and two genuine transitions can share
      a second, so treating a tie as stale would drop real events.

    A first sighting (``cursor is None``) always applies.
    """
    if cursor is None:
        return OrderingDecision(OrderingVerdict.APPLY, "first event for this aggregate", 1)

    incoming_rank = state_rank(event.entity, event.entity_state)
    held_rank = state_rank(cursor.aggregate_type, cursor.last_state)
    if incoming_rank is not None and held_rank is not None and incoming_rank < held_rank:
        return OrderingDecision(
            OrderingVerdict.DISCARD_STALE,
            f"state {event.entity_state!r} precedes held state {cursor.last_state!r}",
            cursor.version,
        )

    if event.occurred_at < cursor.last_event_at:
        return OrderingDecision(
            OrderingVerdict.DISCARD_STALE,
            (
                f"event at {event.occurred_at.isoformat()} predates the last applied "
                f"event at {cursor.last_event_at.isoformat()}"
            ),
            cursor.version,
        )

    return OrderingDecision(
        OrderingVerdict.APPLY, "event advances the aggregate", cursor.next_version
    )


def require_in_order(event: NormalisedEvent, cursor: AggregateCursor | None) -> OrderingDecision:
    """As :func:`check_ordering`, raising :class:`StaleEvent` on a stale delivery.

    ``StaleEvent`` also carries ``http_status = 200``: the delivery was valid and
    is acknowledged, it simply changes nothing.
    """
    decision = check_ordering(event, cursor)
    if not decision.should_apply:
        raise StaleEvent(
            "out-of-order webhook discarded",
            event_id=event.event_id,
            aggregate_id=event.aggregate_id,
            reason=decision.reason,
        )
    return decision


# ------------------------------------------------------------------ the pipeline


@dataclass(frozen=True, slots=True)
class IngestedWebhook:
    """A delivery that is authentic, fresh, and claimed by this process."""

    envelope: WebhookEnvelope
    event: NormalisedEvent
    record: ProcessedWebhook


async def ingest(
    session: AsyncSession,
    *,
    raw_body: bytes,
    headers: Mapping[str, str],
    secret: str,
    clock: Clock,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> IngestedWebhook:
    """Run the four steps in the one order that is correct.

    Raises :class:`WebhookVerificationFailed` (400),
    :class:`~anvil.core.errors.WebhookReplayRejected` (400) or
    :class:`~anvil.core.errors.DuplicateEvent` (200). The ordering guard is *not*
    run here: it needs the aggregate's current version, which only the caller
    holding the domain transaction can supply. Call :func:`require_in_order` with
    the cursor once you have it.
    """
    signature = header_value(headers, SIGNATURE_HEADER) or ""
    require_signature(raw_body, signature, secret)

    envelope = parse_envelope(raw_body, headers)
    check_replay_window(envelope.created_at, clock.now(), tolerance_seconds)

    event = parse_event(envelope)
    record = await claim_event(session, envelope, event, clock=clock)
    return IngestedWebhook(envelope=envelope, event=event, record=record)
