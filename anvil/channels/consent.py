"""The DPDPA gate. Nothing leaves Anvil without passing through it.

The Digital Personal Data Protection Act 2023 does not recognise general
consent. A data principal consents to a *purpose*, having been shown a *notice*,
and may withdraw at any time with the same ease as granting. Anvil models that
literally: :class:`~anvil.db.models.comms.ConsentReceipt` is keyed by
``(customer, purpose, notice_version)``, and the gate below looks up exactly the
purpose the send is about to serve. A grant for
``PAYMENT_FAILURE_NOTICE`` authorises nothing about ``PROMOTIONAL_WINBACK``.

Three design commitments are worth stating, because each of them rules out an
easier implementation:

1. **Fail closed.** :meth:`ConsentGate.require` raises
   :class:`~anvil.core.errors.ConsentMissing` on anything that is not an
   affirmative, currently-effective grant. "No receipt found" and "receipt
   withdrawn" are the same answer at the send boundary; they differ only in what
   gets written down.
2. **Withdrawal never mutates.** Withdrawing writes a *new* receipt in the
   ``WITHDRAWN`` state. The question a regulator asks is not "is this person
   opted in today" -- it is "were you allowed to send the message you sent on
   the 14th", and only an append-only history can answer that.
3. **Withdrawal triggers erasure.** Section 6(6) requires the fiduciary to cease
   processing and erase on withdrawal, save where retention is legally
   mandated. The gate enqueues a purpose-scoped erasure event on the
   transactional outbox in the same transaction as the withdrawal receipt, so a
   crash between the two is not possible. Ledger and audit rows are tombstoned
   rather than deleted, per ``docs/ARCHITECTURE.md`` section 12.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from anvil.core.clock import Clock
from anvil.core.errors import ConsentMissing, ValidationError
from anvil.core.ids import IdPrefix, new_id
from anvil.core.logging import get_logger
from anvil.db.models.comms import ConsentReceipt
from anvil.domain.enums import ConsentState, MessagePurpose

__all__ = [
    "ConsentDecision",
    "ConsentGate",
    "ConsentRepository",
    "ERASURE_TOPIC",
    "OutboxPublisher",
    "resolve_consent",
]

_log = get_logger(__name__)

#: Outbox topic the erasure worker subscribes to. Named here because the
#: producer owns the contract; the consumer lives in another module.
ERASURE_TOPIC = "dpdpa.erasure_requested"


@dataclass(frozen=True, slots=True)
class ConsentDecision:
    """The resolved consent position for one ``(customer, purpose)`` at one instant.

    Carries the *reason* as well as the state because the suppression row a
    dispatcher writes has to say something a human can act on. "no receipt for
    this purpose" and "withdrawn at 2026-03-04T09:11Z" lead to very different
    conversations with a customer.
    """

    customer_id: str
    purpose: MessagePurpose
    state: ConsentState
    at: dt.datetime
    receipt_id: str | None = None
    notice_version: str | None = None
    reason: str = ""

    @property
    def is_effective(self) -> bool:
        return self.state is ConsentState.GRANTED

    def to_audit(self) -> dict[str, Any]:
        return {
            "customer_id": self.customer_id,
            "purpose": self.purpose.value,
            "state": self.state.value,
            "receipt_id": self.receipt_id,
            "notice_version": self.notice_version,
            "evaluated_at": self.at.isoformat(),
            "reason": self.reason,
        }


def resolve_consent(
    receipts: Sequence[ConsentReceipt],
    *,
    customer_id: str,
    purpose: MessagePurpose,
    at: dt.datetime,
) -> ConsentDecision:
    """Reduce a receipt history to a single decision. Pure, total, deterministic.

    The rule, in order:

    * A withdrawal that has already taken effect at ``at`` invalidates every
      grant made *before* it. Re-granting afterwards is a new, valid grant --
      that is what a preference centre does when someone opts back in.
    * Among the grants that survive, the most recent one governs. Ties break on
      receipt id so two receipts stamped at the same microsecond still resolve
      the same way on every machine.
    * With no surviving grant, the state reported is the most informative true
      statement available: ``WITHDRAWN`` if a withdrawal is why, ``EXPIRED`` if a
      grant lapsed, otherwise ``NEVER_GRANTED``.

    Receipts for other purposes are ignored rather than rejected, so a caller
    may hand over a customer's whole consent history without pre-filtering.
    """
    if at.tzinfo is None:
        raise ValidationError("consent must be evaluated at a timezone-aware instant")

    relevant = [r for r in receipts if r.purpose is purpose and r.customer_id == customer_id]

    withdrawals = [
        r.withdrawn_at
        for r in relevant
        if r.state is ConsentState.WITHDRAWN and r.withdrawn_at is not None and r.withdrawn_at <= at
    ]
    latest_withdrawal = max(withdrawals) if withdrawals else None

    live_grants = [r for r in relevant if r.is_effective_at(at)]
    surviving = [
        r
        for r in live_grants
        if latest_withdrawal is None
        or (r.granted_at is not None and r.granted_at > latest_withdrawal)
    ]

    if surviving:
        best = max(surviving, key=lambda r: (r.granted_at or at, r.id))
        return ConsentDecision(
            customer_id=customer_id,
            purpose=purpose,
            state=ConsentState.GRANTED,
            at=at,
            receipt_id=best.id,
            notice_version=best.notice_version,
            reason=f"granted under notice {best.notice_version}",
        )

    if latest_withdrawal is not None:
        return ConsentDecision(
            customer_id=customer_id,
            purpose=purpose,
            state=ConsentState.WITHDRAWN,
            at=at,
            reason=f"consent withdrawn at {latest_withdrawal.isoformat()}",
        )

    lapsed = [
        r
        for r in relevant
        if r.state is ConsentState.GRANTED and r.expires_at is not None and r.expires_at <= at
    ]
    if lapsed:
        newest = max(lapsed, key=lambda r: (r.expires_at or at, r.id))
        expiry = newest.expires_at
        return ConsentDecision(
            customer_id=customer_id,
            purpose=purpose,
            state=ConsentState.EXPIRED,
            at=at,
            receipt_id=newest.id,
            notice_version=newest.notice_version,
            reason=f"consent expired at {expiry.isoformat() if expiry else 'unknown'}",
        )

    return ConsentDecision(
        customer_id=customer_id,
        purpose=purpose,
        state=ConsentState.NEVER_GRANTED,
        at=at,
        reason=f"no consent receipt for purpose {purpose.value}",
    )


class ConsentRepository(Protocol):
    """Persistence for consent receipts.

    Reads return the whole history for a ``(customer, purpose)`` pair rather than
    a pre-reduced answer, because the reduction in :func:`resolve_consent` is the
    part that has to be reviewable and testable without a database.
    """

    async def receipts_for(
        self, customer_id: str, purpose: MessagePurpose
    ) -> Sequence[ConsentReceipt]:
        """Every receipt ever written for this pair, in any order."""
        ...

    async def add(self, receipt: ConsentReceipt) -> None:
        """Stage a new receipt in the caller's transaction. Never updates."""
        ...


class OutboxPublisher(Protocol):
    """The transactional outbox, as this module needs it.

    Deliberately not the ORM row: the channels module publishes an intent and
    should not care how ``anvil.db`` chooses to store it. The implementation must
    enrol in the caller's transaction, otherwise the guarantee that a withdrawal
    and its erasure event commit together is lost.
    """

    async def publish(
        self,
        *,
        topic: str,
        payload: dict[str, Any],
        partition_key: str | None = None,
        available_at: dt.datetime | None = None,
    ) -> str:
        """Stage an outbox entry and return its id."""
        ...


class ConsentGate:
    """Consent lookup, grant and withdrawal for one transaction.

    Holds a repository and a publisher that are both bound to the caller's
    session. It is cheap to construct and is expected to be constructed per
    transaction rather than held as a long-lived service, so there is never a
    question about which transaction a write landed in.
    """

    __slots__ = ("_clock", "_outbox", "_repo")

    def __init__(
        self,
        repository: ConsentRepository,
        clock: Clock,
        *,
        outbox: OutboxPublisher | None = None,
    ) -> None:
        self._repo = repository
        self._clock = clock
        self._outbox = outbox

    async def effective_consent(
        self, customer_id: str, purpose: MessagePurpose, *, at: dt.datetime | None = None
    ) -> ConsentDecision:
        """Answer "may we contact this person for this purpose right now?".

        Never raises for a negative answer. The dispatcher needs the decision as
        a value so it can persist the suppression before anything else happens.
        """
        when = at or self._clock.now()
        receipts = await self._repo.receipts_for(customer_id, purpose)
        return resolve_consent(receipts, customer_id=customer_id, purpose=purpose, at=when)

    async def require(
        self, customer_id: str, purpose: MessagePurpose, *, at: dt.datetime | None = None
    ) -> ConsentDecision:
        """Assert effective consent or refuse. The fail-closed entry point.

        Used by callers that have no suppression-recording path of their own. The
        dispatcher does not use it -- it takes the decision as a value first so
        that the refusal is written down before it is raised.
        """
        decision = await self.effective_consent(customer_id, purpose, at=at)
        if not decision.is_effective:
            raise ConsentMissing(
                f"no effective consent for {purpose.value}: {decision.reason}",
                customer_id=customer_id,
                purpose=purpose.value,
                state=decision.state.value,
            )
        return decision

    async def grant(
        self,
        *,
        merchant_id: str,
        customer_id: str,
        purpose: MessagePurpose,
        notice_version: str,
        notice_summary: str | None = None,
        collection_method: str = "checkout",
        evidence_reference: str | None = None,
        expires_at: dt.datetime | None = None,
        at: dt.datetime | None = None,
    ) -> ConsentReceipt:
        """Record an affirmative grant.

        ``notice_summary`` and ``evidence_reference`` are what make the receipt
        worth having: consent without a record of what the person was shown is
        an assertion, not evidence.
        """
        when = _aware(at or self._clock.now())
        if expires_at is not None:
            expires_at = _aware(expires_at)
            if expires_at <= when:
                raise ValidationError(
                    "a grant cannot expire before it starts", expires_at=expires_at.isoformat()
                )
        if not notice_version.strip():
            raise ValidationError("a grant must name the notice version the principal saw")

        receipt = ConsentReceipt(
            id=new_id(IdPrefix.CONSENT),
            merchant_id=merchant_id,
            customer_id=customer_id,
            purpose=purpose,
            state=ConsentState.GRANTED,
            notice_version=notice_version,
            notice_summary=notice_summary,
            granted_at=when,
            withdrawn_at=None,
            expires_at=expires_at,
            collection_method=collection_method,
            evidence_reference=evidence_reference,
        )
        await self._repo.add(receipt)
        _log.info(
            "consent.granted",
            customer_id=customer_id,
            purpose=purpose.value,
            notice_version=notice_version,
            receipt_id=receipt.id,
        )
        return receipt

    async def withdraw(
        self,
        *,
        merchant_id: str,
        customer_id: str,
        purpose: MessagePurpose,
        reason: str = "principal request",
        collection_method: str = "preference_centre",
        evidence_reference: str | None = None,
        at: dt.datetime | None = None,
    ) -> ConsentReceipt:
        """Record a withdrawal and enqueue the erasure it obliges.

        Writes a new ``WITHDRAWN`` receipt -- the prior grant is left exactly as
        it was, because it is the evidence that earlier sends were lawful. The
        erasure event is purpose-scoped: withdrawing marketing consent does not
        oblige us to forget that an invoice went unpaid, and over-erasing would
        break the financial record the same Act expects us to keep.
        """
        when = _aware(at or self._clock.now())
        receipt = ConsentReceipt(
            id=new_id(IdPrefix.CONSENT),
            merchant_id=merchant_id,
            customer_id=customer_id,
            purpose=purpose,
            state=ConsentState.WITHDRAWN,
            notice_version=_WITHDRAWAL_NOTICE_VERSION,
            notice_summary=reason,
            granted_at=None,
            withdrawn_at=when,
            expires_at=None,
            collection_method=collection_method,
            evidence_reference=evidence_reference,
        )
        await self._repo.add(receipt)

        if self._outbox is not None:
            await self._outbox.publish(
                topic=ERASURE_TOPIC,
                payload={
                    "customer_id": customer_id,
                    "merchant_id": merchant_id,
                    "purpose": purpose.value,
                    "consent_receipt_id": receipt.id,
                    "requested_at": when.isoformat(),
                    "reason": reason,
                    "scope": "purpose",
                },
                partition_key=customer_id,
                available_at=when,
            )
        else:
            _log.warning(
                "consent.withdrawn_without_outbox",
                customer_id=customer_id,
                purpose=purpose.value,
                detail="erasure event not enqueued; gate was built without a publisher",
            )

        _log.info(
            "consent.withdrawn",
            customer_id=customer_id,
            purpose=purpose.value,
            receipt_id=receipt.id,
        )
        return receipt


#: Withdrawals are not made under a notice, but the column is not nullable and a
#: sentinel is more honest than copying the grant's version onto a refusal.
_WITHDRAWAL_NOTICE_VERSION = "withdrawal"


def _aware(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        raise ValidationError("consent timestamps must be timezone-aware")
    return value.astimezone(dt.UTC)
