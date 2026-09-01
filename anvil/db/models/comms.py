"""Outreach, consent and erasure. The DPDPA surface of the system."""

from __future__ import annotations

import datetime as dt
from typing import Any

import sqlalchemy as sa
from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from anvil.db.base import (
    Base,
    CreatedAtMixin,
    MerchantScopedMixin,
    TimestampMixin,
    UTCDateTime,
    pk_column,
)
from anvil.domain.enums import (
    Channel,
    ConsentState,
    DeliveryStatus,
    ErasureStatus,
    MessagePurpose,
)


class ConsentReceipt(Base, TimestampMixin, MerchantScopedMixin):
    """DPDPA consent, recorded per purpose and per notice version.

    Consent is never general. A receipt exists for a specific
    ``(principal, purpose, notice_version)`` triple, and a channel send looks up
    exactly the purpose it is about to serve. Withdrawal is a new row, not an
    update, so the history of what was permitted when is never lost.
    """

    __tablename__ = "consent_receipts"

    id: Mapped[str] = pk_column("cnt")
    customer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    purpose: Mapped[MessagePurpose] = mapped_column(
        sa.Enum(MessagePurpose, native_enum=False, length=40), nullable=False, index=True
    )
    state: Mapped[ConsentState] = mapped_column(
        sa.Enum(ConsentState, native_enum=False, length=24), nullable=False, index=True
    )
    notice_version: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Free text describing exactly what the principal was shown.
    notice_summary: Mapped[str | None] = mapped_column(Text)

    granted_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    withdrawn_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    expires_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, index=True)

    #: How the principal expressed it -- checkout flow, preference centre, reply.
    collection_method: Mapped[str] = mapped_column(String(48), nullable=False, default="checkout")
    #: Evidence handle. Never the raw artifact.
    evidence_reference: Mapped[str | None] = mapped_column(String(96))

    __table_args__ = (
        Index("ix_consent_lookup", "customer_id", "purpose", "state"),
        sa.CheckConstraint(
            "(state <> 'granted') OR (granted_at IS NOT NULL)", name="granted_needs_timestamp"
        ),
    )

    def is_effective_at(self, when: dt.datetime) -> bool:
        if self.state is not ConsentState.GRANTED:
            return False
        if self.granted_at is None or self.granted_at > when:
            return False
        if self.withdrawn_at is not None and self.withdrawn_at <= when:
            return False
        return not (self.expires_at is not None and self.expires_at <= when)


class OutreachMessage(Base, TimestampMixin, MerchantScopedMixin):
    """One outbound message, from composition through delivery or suppression.

    Suppressed messages are stored, not discarded. "We did not contact this
    customer because consent was withdrawn" is exactly the record a regulator
    asks for, and throwing it away is how a compliant system becomes an
    unprovable one.
    """

    __tablename__ = "outreach_messages"

    id: Mapped[str] = pk_column("msg")
    case_id: Mapped[str | None] = mapped_column(String(32), index=True)
    action_id: Mapped[str | None] = mapped_column(String(32), index=True)
    customer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    channel: Mapped[Channel] = mapped_column(
        sa.Enum(Channel, native_enum=False, length=16), nullable=False, index=True
    )
    purpose: Mapped[MessagePurpose] = mapped_column(
        sa.Enum(MessagePurpose, native_enum=False, length=40), nullable=False, index=True
    )
    status: Mapped[DeliveryStatus] = mapped_column(
        sa.Enum(DeliveryStatus, native_enum=False, length=32),
        nullable=False,
        default=DeliveryStatus.QUEUED,
        index=True,
    )

    language: Mapped[str] = mapped_column(String(8), nullable=False, default="en")
    subject: Mapped[str | None] = mapped_column(String(300))
    #: Rendered body with PII already tokenised. Rehydrated only at send time.
    body_redacted: Mapped[str] = mapped_column(Text, nullable=False)
    #: Which consent receipt authorised this send. Null only when suppressed.
    consent_receipt_id: Mapped[str | None] = mapped_column(String(32))
    suppression_reason: Mapped[str | None] = mapped_column(Text)

    idempotency_key: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    cost_minor: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)

    queued_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    sent_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    delivered_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    responded_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    #: Set when the customer took the action the message asked for.
    converted: Mapped[bool] = mapped_column(nullable=False, default=False, index=True)

    __table_args__ = (
        Index("ix_messages_customer_sent", "customer_id", "sent_at"),
        Index("ix_messages_frequency_window", "customer_id", "queued_at"),
        sa.CheckConstraint("cost_minor >= 0", name="message_cost_non_negative"),
    )

    @property
    def was_suppressed(self) -> bool:
        return self.status.value.startswith("suppressed")


class ErasureRequest(Base, TimestampMixin, MerchantScopedMixin):
    """A DPDPA right-to-erasure request, worked asynchronously with a DLQ.

    Financial records are not deleted. They are tombstoned: PII is replaced with
    irreversible tokens while the ledger and audit rows stay intact, which
    honours erasure without destroying the books.
    """

    __tablename__ = "erasure_requests"

    id: Mapped[str] = pk_column("ers")
    customer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[ErasureStatus] = mapped_column(
        sa.Enum(ErasureStatus, native_enum=False, length=24),
        nullable=False,
        default=ErasureStatus.REQUESTED,
        index=True,
    )
    requested_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    completed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    #: Per-target progress: which stores have been purged and which have not.
    targets: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    attempts: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=0)
    next_attempt_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    dead_lettered_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        Index("ix_erasure_due", "status", "next_attempt_at"),
        sa.CheckConstraint("attempts >= 0", name="erasure_attempts_non_negative"),
    )


class ContactLedger(Base, CreatedAtMixin, MerchantScopedMixin):
    """Append-only record of every contact, for frequency-cap arithmetic.

    Separate from :class:`OutreachMessage` so the cap can be enforced with one
    narrow index scan, and so the cap survives message retention policies.
    """

    __tablename__ = "contact_ledger"

    id: Mapped[str] = pk_column("ctl")
    customer_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    channel: Mapped[Channel] = mapped_column(
        sa.Enum(Channel, native_enum=False, length=16), nullable=False
    )
    purpose: Mapped[MessagePurpose] = mapped_column(
        sa.Enum(MessagePurpose, native_enum=False, length=40), nullable=False
    )
    contacted_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    message_id: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (Index("ix_contact_customer_time", "customer_id", "contacted_at"),)
