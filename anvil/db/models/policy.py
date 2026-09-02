"""Versioned policy bundles, their rules, evaluations, and human approvals."""

from __future__ import annotations

import datetime as dt
from typing import Any

import sqlalchemy as sa
from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from anvil.db.base import (
    Base,
    CreatedAtMixin,
    CurrencyType,
    MerchantScopedMixin,
    TimestampMixin,
    UTCDateTime,
    pk_column,
)
from anvil.domain.enums import (
    ApprovalDecision,
    InterruptKind,
    PolicyBundleStatus,
    PolicyEffect,
)
from anvil.domain.money import Currency


class PolicyBundle(Base, TimestampMixin, MerchantScopedMixin):
    """An immutable, content-addressed set of rules.

    A bundle is never edited. Compiling merchant prose produces a *new* bundle
    in ``PROPOSED``; a human reviews the diff and activates it, which supersedes
    the previous one. ``content_hash`` makes "is this the bundle that was
    approved?" a byte comparison rather than a judgement.
    """

    __tablename__ = "policy_bundles"

    id: Mapped[str] = pk_column("pol")
    version: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[PolicyBundleStatus] = mapped_column(
        sa.Enum(PolicyBundleStatus, native_enum=False, length=24),
        nullable=False,
        default=PolicyBundleStatus.DRAFT,
        index=True,
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    #: The prose the merchant wrote, when this bundle came from the compiler.
    source_text: Mapped[str | None] = mapped_column(Text)
    #: Which model produced it, and the call id, so compilation is auditable.
    compiled_by_model: Mapped[str | None] = mapped_column(String(64))
    compiled_from_call_id: Mapped[str | None] = mapped_column(String(32))
    #: Plain-language summary of what changed versus the bundle it supersedes.
    diff_summary: Mapped[str | None] = mapped_column(Text)

    supersedes_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("policy_bundles.id", ondelete="RESTRICT")
    )
    activated_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    activated_by: Mapped[str | None] = mapped_column(String(120))
    rejected_reason: Mapped[str | None] = mapped_column(Text)

    rules: Mapped[list[PolicyRule]] = relationship(
        back_populates="bundle", lazy="selectin", order_by="PolicyRule.priority"
    )

    __table_args__ = (
        sa.UniqueConstraint("merchant_id", "version", name="uq_bundle_merchant_version"),
        Index("ix_bundles_merchant_status", "merchant_id", "status"),
        sa.CheckConstraint("version > 0", name="bundle_version_positive"),
    )

    @property
    def is_active(self) -> bool:
        return self.status is PolicyBundleStatus.ACTIVE


class PolicyRule(Base, CreatedAtMixin):
    """One rule: a typed predicate over facts, with an effect.

    ``conditions`` is a small JSON expression tree, not code. It cannot call
    anything, cannot loop, and cannot reach the network -- evaluation is total
    and side-effect free, which is why a rule can be trusted to be the same
    thing every time it runs.
    """

    __tablename__ = "policy_rules"

    id: Mapped[str] = pk_column("prl")
    bundle_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("policy_bundles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Lower runs first. The first matching DENY wins outright.
    priority: Mapped[int] = mapped_column(nullable=False, default=100)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    effect: Mapped[PolicyEffect] = mapped_column(
        sa.Enum(PolicyEffect, native_enum=False, length=24), nullable=False
    )
    conditions: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    #: For CAP rules: the ceiling this rule imposes.
    cap_amount_minor: Mapped[int | None] = mapped_column(sa.BigInteger)
    cap_percent: Mapped[int | None] = mapped_column(sa.SmallInteger)
    currency: Mapped[Currency] = mapped_column(CurrencyType, nullable=False, default=Currency.INR)

    #: A rule the merchant cannot compile away. Regulatory floors live here.
    is_immutable: Mapped[bool] = mapped_column(nullable=False, default=False)

    bundle: Mapped[PolicyBundle] = relationship(back_populates="rules", lazy="raise")

    __table_args__ = (
        Index("ix_rules_bundle_priority", "bundle_id", "priority"),
        sa.CheckConstraint(
            "cap_percent IS NULL OR cap_percent BETWEEN 0 AND 100", name="rule_cap_percent_range"
        ),
    )


class PolicyEvaluation(Base, CreatedAtMixin, MerchantScopedMixin):
    """The recorded result of one evaluation. Append-only.

    Invariant 7: an action cannot execute without one of these, and the row
    names the exact bundle and rule that permitted it. This is what makes
    "why was this allowed?" answerable months later.
    """

    __tablename__ = "policy_evaluations"

    id: Mapped[str] = pk_column("pev")
    case_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    action_id: Mapped[str | None] = mapped_column(String(32), index=True)
    bundle_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    bundle_version: Mapped[int] = mapped_column(nullable=False)

    effect: Mapped[PolicyEffect] = mapped_column(
        sa.Enum(PolicyEffect, native_enum=False, length=24), nullable=False, index=True
    )
    matched_rule_id: Mapped[str | None] = mapped_column(String(32))
    matched_rule_name: Mapped[str | None] = mapped_column(String(160))
    #: Every rule that fired, in order, so a surprising outcome can be traced.
    trace: Mapped[list[dict[str, Any]]] = mapped_column(nullable=False, default=list)
    #: The exact facts evaluated, so the decision can be replayed bit for bit.
    facts: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    capped_amount_minor: Mapped[int | None] = mapped_column(sa.BigInteger)

    __table_args__ = (Index("ix_evaluations_case_created", "case_id", "created_at"),)


class Approval(Base, TimestampMixin, MerchantScopedMixin):
    """A human decision the graph is blocked on.

    ``version`` gives optimistic locking. Two operators opening the same item
    both see version 1; the first to resolve it writes version 2, and the second
    gets a conflict and a refreshed view rather than a silent double-approval.
    """

    __tablename__ = "approvals"

    id: Mapped[str] = pk_column("apr")
    case_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    action_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[InterruptKind] = mapped_column(
        sa.Enum(InterruptKind, native_enum=False, length=32),
        nullable=False,
        default=InterruptKind.HUMAN_APPROVAL,
    )

    #: Everything the operator needs to decide, snapshotted at request time so
    #: the queue renders identically no matter what changes underneath it.
    presented_summary: Mapped[str] = mapped_column(Text, nullable=False)
    presented_rationale: Mapped[str | None] = mapped_column(Text)
    presented_payload: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    amount_minor: Mapped[int | None] = mapped_column(sa.BigInteger)
    currency: Mapped[Currency] = mapped_column(CurrencyType, nullable=False, default=Currency.INR)
    #: Why a human is being asked at all -- which rule escalated this.
    escalation_reason: Mapped[str] = mapped_column(Text, nullable=False)

    requested_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, index=True)

    decision: Mapped[ApprovalDecision | None] = mapped_column(
        sa.Enum(ApprovalDecision, native_enum=False, length=16), index=True
    )
    decided_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    decided_by: Mapped[str | None] = mapped_column(String(120))
    decision_note: Mapped[str | None] = mapped_column(Text)
    #: When the operator edited rather than plainly approving.
    edited_payload: Mapped[dict[str, Any] | None] = mapped_column()

    version: Mapped[int] = mapped_column(nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version}

    __table_args__ = (Index("ix_approvals_pending", "merchant_id", "decision", "requested_at"),)

    @property
    def is_pending(self) -> bool:
        return self.decision is None
