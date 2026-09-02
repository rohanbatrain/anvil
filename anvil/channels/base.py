"""The channel boundary: what a message is, what an adapter promises, what a send returns.

Everything downstream of this module -- the consent gate, the frequency
enforcer, the dispatcher -- reasons about :class:`OutboundMessage` and
:class:`DeliveryResult` and never about a provider SDK. That is deliberate. The
compliance argument Anvil has to make ("this send was permitted, and here is the
evidence") must hold identically whether the message went out over SMTP, over a
WhatsApp Business API, or into a local file during a demo, and it can only do
that if the permission logic cannot see the difference.

Two things are modelled here that a naive channel layer leaves implicit:

* **Medium.** A send that landed in a directory on a laptop is not a send that
  reached a person. :class:`DeliveryMedium` records which happened and stamps it
  into the provider id, so no row in ``outreach_messages`` can later be read as
  evidence of a delivery that never occurred.
* **Cost.** Channel spend is real money and belongs in the ledger next to
  concessions and model spend, otherwise "cost per recovered rupee" is a
  fiction. Every adapter therefore has to price a message before it sends it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from anvil.core.errors import NotFound, ValidationError
from anvil.domain.enums import Channel, DeliveryStatus, MessagePurpose
from anvil.domain.money import Currency, Money

__all__ = [
    "ChannelAdapter",
    "ChannelCapabilities",
    "ChannelRegistry",
    "DeliveryMedium",
    "DeliveryResult",
    "OutboundMessage",
    "Recipient",
    "SENDABLE_STATUSES",
]


class DeliveryMedium(StrEnum):
    """How a send physically happened.

    This is an operational detail of the channels module, not part of the closed
    domain vocabulary in :mod:`anvil.domain.enums` -- the domain cares that a
    message was ``SENT``, the auditor cares *by what means*. Keeping the two
    apart means the demo can run entirely on simulated and local-file media
    without a single row in the database claiming a real delivery.
    """

    SMTP = "smtp"
    LOCAL_FILE = "local_file"
    SIMULATED = "simulated"

    @property
    def reaches_a_real_recipient(self) -> bool:
        """True only for media that actually hand the message to a network.

        A file on disk is a genuine artifact and a genuine record of intent. It
        is not a delivery, and the distinction is exactly the one a regulator
        or a judge would probe.
        """
        return self is DeliveryMedium.SMTP


#: Provider ids are prefixed with the medium in upper case. A grep for
#: ``SIMULATED-`` over ``outreach_messages`` is therefore a complete inventory of
#: everything the demo pretended to send.
PROVIDER_ID_PREFIX: Mapping[DeliveryMedium, str] = {
    DeliveryMedium.SMTP: "SMTP",
    DeliveryMedium.LOCAL_FILE: "LOCAL-OUTBOX",
    DeliveryMedium.SIMULATED: "SIMULATED",
}

#: Statuses an adapter is allowed to return. Suppression statuses are decided by
#: the dispatcher before an adapter is ever consulted, so an adapter that
#: returned one would be reporting a decision it is not entitled to make.
SENDABLE_STATUSES: frozenset[DeliveryStatus] = frozenset(
    {
        DeliveryStatus.SENT,
        DeliveryStatus.DELIVERED,
        DeliveryStatus.QUEUED,
        DeliveryStatus.FAILED,
        DeliveryStatus.BOUNCED,
    }
)


@dataclass(frozen=True, slots=True)
class Recipient:
    """Where a message is addressed, plus the pseudonym it is stored under.

    Anvil's ``customers`` table holds only tokens and display hints, never a raw
    email address or phone number. ``address`` is therefore supplied by whoever
    is entitled to resolve the token at send time -- the simulator in offline
    mode, the merchant's own directory in live mode -- and is never persisted by
    this module. ``token`` is what goes into logs and audit rows.
    """

    address: str
    display_name: str | None = None
    token: str | None = None

    def __post_init__(self) -> None:
        if not self.address.strip():
            raise ValidationError("recipient address must not be empty")

    @property
    def audit_handle(self) -> str:
        """The identifier safe to write into a log or an audit record."""
        return self.token or "unresolved-recipient"


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """A composed message that has not yet been permitted to leave.

    Construction is cheap and side-effect free: composing a message is not the
    same act as being allowed to send it, and keeping them separate is what lets
    the dispatcher persist "we composed this and then declined to send it, for
    this reason" as a first-class record.

    ``time_critical`` is the single lever a caller has over quiet hours, and it
    is deliberately narrow. It asserts that a human being is at that moment
    waiting on this message to finish something they started -- a step-up
    challenge for a payment the customer just tapped. It is not a priority flag
    and it does not override consent or the frequency caps. See
    :mod:`anvil.channels.frequency` for the rule it feeds.
    """

    merchant_id: str
    customer_id: str
    channel: Channel
    purpose: MessagePurpose
    recipient: Recipient
    body: str
    idempotency_key: str
    subject: str | None = None
    language: str = "en"
    case_id: str | None = None
    action_id: str | None = None
    time_critical: bool = False
    #: A further-redacted body for persistence, when the composer produced one.
    redacted_body: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("merchant_id", "customer_id", "idempotency_key"):
            if not str(getattr(self, name)).strip():
                raise ValidationError(f"OutboundMessage.{name} must not be empty")
        if not self.body.strip():
            raise ValidationError("OutboundMessage.body must not be empty")
        if self.time_critical and not self.purpose.is_transactional:
            raise ValidationError(
                "a promotional message can never be time-critical",
                purpose=self.purpose.value,
            )

    @property
    def body_for_storage(self) -> str:
        """What goes into ``outreach_messages.body_redacted``.

        Falls back to the rendered body because Anvil only ever renders from
        tokens and hints -- there is no raw identifier in a template-composed
        body to begin with. The override exists for the LLM composer path, which
        may have had more to redact.
        """
        return self.redacted_body if self.redacted_body is not None else self.body

    @property
    def is_promotional(self) -> bool:
        return not self.purpose.is_transactional


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """What an adapter reports back about one send attempt.

    The cost travels with the result rather than being looked up afterwards
    because pricing can depend on the message -- a WhatsApp marketing
    conversation costs several times a utility one -- and the number that lands
    in the ledger must be the number the adapter actually acted on.
    """

    provider_message_id: str
    status: DeliveryStatus
    cost: Money
    medium: DeliveryMedium
    channel: Channel
    sent_at: datetime | None = None
    error: str | None = None
    detail: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in SENDABLE_STATUSES:
            raise ValidationError(
                "adapters may not return a suppression status; that is the dispatcher's call",
                status=self.status.value,
            )
        if self.cost.is_negative:
            raise ValidationError("channel cost cannot be negative", minor=self.cost.minor)
        expected = PROVIDER_ID_PREFIX[self.medium]
        if not self.provider_message_id.startswith(expected):
            raise ValidationError(
                "provider id must name its medium so a simulated send can never read as real",
                provider_message_id=self.provider_message_id,
                expected_prefix=expected,
            )
        if self.succeeded and self.sent_at is None:
            raise ValidationError("a successful delivery must carry the instant it happened")
        if self.sent_at is not None and self.sent_at.tzinfo is None:
            raise ValidationError("DeliveryResult.sent_at must be timezone-aware")

    @property
    def succeeded(self) -> bool:
        return self.status in (DeliveryStatus.SENT, DeliveryStatus.DELIVERED)

    @property
    def is_simulated(self) -> bool:
        return self.medium is DeliveryMedium.SIMULATED

    @property
    def billable_cost(self) -> Money:
        """Cost to post to the ledger.

        A failed send is not billed. Providers do charge for some hard bounces,
        but attributing a cost to a message that never left overstates channel
        spend and therefore understates the recovery economics, and this project
        would rather understate its own numbers than overstate them.
        """
        return self.cost if self.succeeded else Money.zero(self.cost.currency)


@dataclass(frozen=True, slots=True)
class ChannelCapabilities:
    """What a channel can carry, so the composer does not have to guess.

    ``max_body_chars`` is enforced by the adapter rather than trusted: silently
    truncating a payment link into an unusable half-link is worse than a
    recorded failure, because the failure is visible and the truncation is not.
    """

    channel: Channel
    medium: DeliveryMedium
    supports_subject: bool
    supports_rich_body: bool
    max_body_chars: int
    requires_pre_approved_template: bool = False
    supports_attachments: bool = False

    def __post_init__(self) -> None:
        if self.max_body_chars < 1:
            raise ValidationError("max_body_chars must be positive", channel=self.channel.value)


class ChannelAdapter(Protocol):
    """The contract every outbound channel satisfies.

    Narrow on purpose. An adapter prices a message and sends it; it does not
    decide whether the message may be sent, does not touch the database, and
    does not know what a consent receipt is. Every permission question is
    settled before ``send`` is called, which is what makes "no send happens
    without a consent check" a structural property rather than a code-review
    convention.
    """

    @property
    def channel(self) -> Channel:
        """Which channel this adapter serves. One adapter per channel."""
        ...

    @property
    def capabilities(self) -> ChannelCapabilities:
        """Static limits and flags for this channel."""
        ...

    def cost_for(self, message: OutboundMessage) -> Money:
        """Price this specific message, in integer minor units.

        Takes the message rather than being a constant because real channel
        pricing is per-category: a WhatsApp marketing template and a WhatsApp
        authentication template are billed differently by Meta, and pretending
        otherwise would put a wrong number in the ledger.
        """
        ...

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        """Hand the message to the medium. Must not raise for a provider refusal.

        A provider saying no is data -- it becomes a ``FAILED`` or ``BOUNCED``
        row with a reason. Only a programming error should escape as an
        exception, because an exception loses the record.
        """
        ...


class ChannelRegistry:
    """Adapter lookup by channel, total over whatever it was built with.

    A registry rather than a module-level dict so a test, the simulator and the
    live worker can each hold their own set of adapters at the same time without
    reaching through global state.
    """

    __slots__ = ("_adapters",)

    def __init__(self, adapters: Iterable[ChannelAdapter]) -> None:
        mapping: dict[Channel, ChannelAdapter] = {}
        for adapter in adapters:
            if adapter.channel in mapping:
                raise ValidationError(
                    "two adapters claim the same channel", channel=adapter.channel.value
                )
            mapping[adapter.channel] = adapter
        self._adapters = mapping

    def get(self, channel: Channel) -> ChannelAdapter:
        adapter = self._adapters.get(channel)
        if adapter is None:
            raise NotFound(f"no adapter registered for channel {channel.value}", channel=channel)
        return adapter

    def __contains__(self, channel: Channel) -> bool:
        return channel in self._adapters

    def channels(self) -> tuple[Channel, ...]:
        return tuple(self._adapters)

    def cost_table(self, message: OutboundMessage) -> dict[Channel, Money]:
        """What this message would cost on every registered channel.

        The planner uses it to choose the cheapest channel that can carry the
        content, which is a deterministic decision and stays out of the model.
        """
        return {
            channel: adapter.cost_for(message) for channel, adapter in self._adapters.items()
        }


def zero_cost(currency: Currency = Currency.INR) -> Money:
    """Convenience for adapters and fakes that bill nothing."""
    return Money.zero(currency)
