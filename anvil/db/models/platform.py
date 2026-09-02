"""Platform plumbing: the event log, outbox, audit trail, webhook dedupe and LLM call log.

These tables are what make the system replayable and provable. Three of them are
strictly append-only.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from anvil.db.base import Base, CreatedAtMixin, TimestampMixin, UTCDateTime, pk_column
from anvil.domain.enums import AuditEventType, LLMCallKind


class DomainEvent(Base, CreatedAtMixin):
    """The append-only event log. Written in the same transaction as the state change.

    Because the event and the read-model row commit together, the log can never
    disagree with the state it describes -- which is the property that buys
    event sourcing's replay guarantees without event sourcing's eventual
    consistency.
    """

    __tablename__ = "domain_events"

    id: Mapped[str] = pk_column("evt")
    #: Global monotonic ordering. Assigned by the database, never by the app.
    sequence: Mapped[int] = mapped_column(
        sa.BigInteger, sa.Identity(always=True), nullable=False, unique=True, index=True
    )
    merchant_id: Mapped[str | None] = mapped_column(String(32), index=True)
    aggregate_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    aggregate_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: Per-aggregate version, for detecting out-of-order application.
    aggregate_version: Mapped[int] = mapped_column(nullable=False, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    #: Correlation across a whole recovery journey; causation to the direct parent.
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    causation_id: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, index=True)

    __table_args__ = (
        sa.UniqueConstraint(
            "aggregate_type", "aggregate_id", "aggregate_version", name="uq_event_aggregate_version"
        ),
        Index("ix_events_aggregate_seq", "aggregate_type", "aggregate_id", "sequence"),
    )


class OutboxEntry(Base, TimestampMixin):
    """Transactional outbox. Written with the state change, relayed afterwards.

    This is why a worker crash cannot lose a scheduled action: the intent to do
    something is committed in the same transaction as the fact that justified
    it, so either both survive or neither does.
    """

    __tablename__ = "outbox"

    id: Mapped[str] = pk_column("obx")
    topic: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    #: Messages for the same key are relayed in order.
    partition_key: Mapped[str | None] = mapped_column(String(64), index=True)

    available_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    claimed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    claimed_by: Mapped[str | None] = mapped_column(String(64))
    published_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    attempts: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=8)
    last_error: Mapped[str | None] = mapped_column(Text)
    dead_lettered_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, index=True)

    __table_args__ = (
        Index("ix_outbox_claimable", "published_at", "available_at"),
        sa.CheckConstraint("attempts >= 0", name="outbox_attempts_non_negative"),
    )

    @property
    def is_pending(self) -> bool:
        return self.published_at is None and self.dead_lettered_at is None


class AuditRecord(Base, CreatedAtMixin):
    """Immutable compliance trail. Append-only, and free of raw PII by construction.

    Invariant 10. Redaction happens on the way in. A record that reached this
    table with a phone number in it would be a bug that no amount of careful
    reading later could undo, so the write path is the only place the rule is
    enforced.
    """

    __tablename__ = "audit_records"

    id: Mapped[str] = pk_column("adt")
    sequence: Mapped[int] = mapped_column(
        sa.BigInteger, sa.Identity(always=True), nullable=False, unique=True, index=True
    )
    merchant_id: Mapped[str | None] = mapped_column(String(32), index=True)
    case_id: Mapped[str | None] = mapped_column(String(32), index=True)
    action_id: Mapped[str | None] = mapped_column(String(32), index=True)

    event_type: Mapped[AuditEventType] = mapped_column(
        sa.Enum(AuditEventType, native_enum=False, length=48), nullable=False, index=True
    )
    #: Who caused it: ``agent``, ``system``, or an operator identity.
    actor: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="system")
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    #: Structured, already redacted.
    detail: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)

    #: Graph checkpoint this record corresponds to, enabling time-travel from
    #: any audit row straight into the state that produced it.
    thread_id: Mapped[str | None] = mapped_column(String(64), index=True)
    checkpoint_id: Mapped[str | None] = mapped_column(String(64))

    occurred_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, index=True)

    __table_args__ = (
        Index("ix_audit_case_seq", "case_id", "sequence"),
        Index("ix_audit_merchant_type_time", "merchant_id", "event_type", "occurred_at"),
        sa.CheckConstraint(
            "actor_kind IN ('agent','system','operator','customer')", name="audit_actor_kind_valid"
        ),
    )


class ProcessedWebhook(Base, CreatedAtMixin):
    """Webhook dedupe. Invariant 4.

    The unique constraint on ``event_id`` *is* the idempotency mechanism. A
    duplicate delivery raises a constraint violation, which the handler
    translates into a plain ``200 OK`` without re-running anything.
    """

    __tablename__ = "processed_webhooks"

    id: Mapped[str] = pk_column("whk")
    #: Razorpay's ``x-razorpay-event-id`` header.
    event_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: Payload timestamp, checked against the replay tolerance window.
    event_timestamp: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    received_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    processed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    #: Digest of the raw body, so a replay with a mutated body is detectable.
    body_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), index=True)
    processing_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_webhooks_type_received", "event_type", "received_at"),)


class IdempotencyRecord(Base, TimestampMixin):
    """Server-side record of a caller-supplied idempotency key.

    Covers our own API surface. Outbound keys to Razorpay are recorded on the
    action and attempt rows instead, where they belong to the thing they protect.
    """

    __tablename__ = "idempotency_records"

    id: Mapped[str] = pk_column("idm")
    key: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    scope: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int | None] = mapped_column(sa.SmallInteger)
    response_body: Mapped[dict[str, Any] | None] = mapped_column()
    completed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    expires_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, index=True)


class LLMCall(Base, CreatedAtMixin):
    """Every model call, recorded. Prompts stored redacted.

    Cost and latency here feed the "cost per recovered rupee" figure the
    evidence harness reports, so the economics of the agent are measured rather
    than asserted.
    """

    __tablename__ = "llm_calls"

    id: Mapped[str] = pk_column("llm")
    case_id: Mapped[str | None] = mapped_column(String(32), index=True)
    kind: Mapped[LLMCallKind] = mapped_column(
        sa.Enum(LLMCallKind, native_enum=False, length=24), nullable=False, index=True
    )
    model: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    #: Redacted before persistence, always.
    prompt_redacted: Mapped[str] = mapped_column(Text, nullable=False)
    response_raw: Mapped[str | None] = mapped_column(Text)
    parsed_output: Mapped[dict[str, Any] | None] = mapped_column()

    input_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    cost_minor: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(nullable=False, default=0)

    attempt: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=1)
    succeeded: Mapped[bool] = mapped_column(nullable=False, default=True, index=True)
    validation_error: Mapped[str | None] = mapped_column(Text)
    #: True when this response came from a recorded fixture rather than the API.
    from_fixture: Mapped[bool] = mapped_column(nullable=False, default=False, index=True)
    fixture_key: Mapped[str | None] = mapped_column(String(96), index=True)

    __table_args__ = (
        Index("ix_llm_calls_kind_created", "kind", "created_at"),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0", name="llm_tokens_non_negative"
        ),
        sa.CheckConstraint("cost_minor >= 0", name="llm_cost_non_negative"),
    )
