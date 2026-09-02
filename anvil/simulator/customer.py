"""The customer model: how a person on the other end of a failed debit behaves.

The diagnosis task in Anvil is only real if there is something genuinely hidden
to infer. So every simulated customer carries two latent variables that nothing
downstream is ever allowed to read:

* **ability to pay** -- whether the money is actually in the account. It drives
  the issuer's balance-driven declines and the payoff to waiting for payday.
* **intent to pay** -- whether they still want the subscription. It drives
  whether outreach converts, whether a concession lands, and how close they
  already are to leaving.

An insufficient-funds decline from a high-intent, low-ability customer and one
from a low-intent, high-ability customer look *identical* on the wire. Only the
surrounding evidence -- tenure, prior recoveries, how they answered last time --
separates them. That separation is exactly what the diagnosis node is being
scored on, and it is why these two numbers are generated here and never exposed
through the event stream.

Two behavioural facts are modelled deliberately because most dunning systems
ignore them and pay for it:

* **Contact fatigue.** Each message makes the next one less likely to be read.
* **Contact-driven churn.** Each message also raises the hazard of the customer
  leaving outright. Pestering someone is not free, and a recovery engine that
  treats outreach as costless will happily burn the relationship to win the
  invoice. The churn hazard here rises monotonically with contact count so that
  the planner's frequency caps have something real to protect against.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from decimal import Decimal

from anvil.core.clock import ist_hour
from anvil.domain.enums import Channel, MessagePurpose
from anvil.domain.money import Money
from anvil.simulator.rng import (
    ONE,
    ZERO,
    bernoulli,
    clamp_unit,
    substream,
    to_bps,
)

# --------------------------------------------------------------------- tuning
# Every constant here is a generative parameter of the simulated world. They are
# module-level and named so that a reader can argue with them, which is more
# useful than burying them as literals in the middle of a formula.

#: Each prior contact multiplies read probability by 1 / (1 + k * contacts).
CONTACT_FATIGUE_K = Decimal("0.35")
#: A message in a language the customer did not choose still gets read sometimes.
WRONG_LANGUAGE_FACTOR = Decimal("0.62")
#: Outreach that misdiagnoses the cause -- dunning someone whose card expired --
#: converts far worse even when it is read.
IRRELEVANT_MESSAGE_FACTOR = Decimal("0.45")
#: Messages that land inside quiet hours are technically delivered and mostly
#: ignored. The policy engine should be preventing these; the simulator prices
#: them so that failing to prevent them costs something measurable.
QUIET_HOUR_FACTOR = Decimal("0.30")
QUIET_HOURS_START_IST = 21
QUIET_HOURS_END_IST = 8

#: Concession acceptance saturates: the first rupee of relief buys far more
#: goodwill than the fiftieth. ``r / (r + k)`` with ``r`` the concession as a
#: fraction of monthly value is the simplest curve with that shape.
CONCESSION_HALF_SATURATION = Decimal("0.18")
CONCESSION_FLOOR_ACCEPTANCE = Decimal("0.30")
CONCESSION_INTENT_WEIGHT = Decimal("0.55")

#: Churn hazard multipliers. Contacts hurt more than failures because a failed
#: debit is a bank event and a badly-timed message is the merchant's fault.
CHURN_PER_CONTACT = Decimal("0.28")
CHURN_PER_FAILED_ATTEMPT = Decimal("0.18")
CHURN_INTENT_SPAN = Decimal("1.60")
MAX_CHURN_HAZARD = Decimal("0.95")


@dataclass(frozen=True, slots=True)
class CustomerTraits:
    """The latent state of one simulated customer.

    Frozen because these are ground truth: nothing in a run may adjust a
    customer's true intent to make a recovery look better after the fact.
    """

    customer_id: str
    language: str
    #: Wants to keep paying. Drives conversion, concession uptake and churn.
    intent_to_pay: Decimal
    #: Has the money. Drives balance-driven declines and payday recovery.
    ability_to_pay: Decimal
    #: Base probability of reading a well-targeted message on a good channel.
    responsiveness: Decimal
    #: Probability of completing a self-service journey once they decide to.
    digital_confidence: Decimal
    #: How strongly a concession moves them, relative to its size.
    price_sensitivity: Decimal
    #: Per-episode churn hazard before any contact or failure pressure.
    baseline_churn: Decimal
    #: Relative pull of each channel. WhatsApp beats email for most of India.
    channel_affinity: tuple[tuple[Channel, Decimal], ...]

    def affinity_for(self, channel: Channel) -> Decimal:
        """Affinity for a channel, defaulting low for channels never opted into."""
        for candidate, weight in self.channel_affinity:
            if candidate is channel:
                return weight
        return Decimal("0.20")

    @property
    def preferred_channel(self) -> Channel:
        """The channel the customer answers on. Ties break on enum order."""
        best_channel = Channel.EMAIL
        best_weight = Decimal(-1)
        for candidate, weight in self.channel_affinity:
            if weight > best_weight:
                best_channel, best_weight = candidate, weight
        return best_channel


@dataclass(frozen=True, slots=True)
class CustomerState:
    """Everything about a customer that a recovery episode can change.

    Immutable with copy-on-write transitions, so a caller can hold on to the
    state as it was at the moment a decision was taken. That matters for
    scoring: "was this contact the fourth one?" must be answerable after the
    fact, not just while the counter happens to hold that value.
    """

    contacts_made: int = 0
    messages_read: int = 0
    failed_attempts: int = 0
    concessions_offered: int = 0
    concessions_accepted: int = 0
    instrument_updated: bool = False
    mandate_reauthorised: bool = False
    churned: bool = False

    def after_contact(self, *, was_read: bool) -> CustomerState:
        return replace(
            self,
            contacts_made=self.contacts_made + 1,
            messages_read=self.messages_read + (1 if was_read else 0),
        )

    def after_failed_attempt(self) -> CustomerState:
        return replace(self, failed_attempts=self.failed_attempts + 1)


@dataclass(frozen=True, slots=True)
class Outreach:
    """One message as the customer experiences it, not as the system sent it."""

    #: Stable identity of this message. Seeds the response draw, so the same
    #: message always gets the same reaction however often it is evaluated.
    message_key: str
    channel: Channel
    purpose: MessagePurpose
    language: str
    at: dt.datetime
    #: Monthly value of the subscription the message is about. The reference
    #: point against which any concession is judged.
    mrr: Money
    concession: Money | None = None
    #: True when the message speaks to the actual reason the debit failed.
    #: A dunning notice sent to someone whose card expired does not.
    addresses_true_cause: bool = True


@dataclass(frozen=True, slots=True)
class CustomerResponse:
    """What the customer did, plus the probabilities that produced it.

    The probabilities are returned rather than discarded because the evidence
    harness needs to distinguish "the plan was bad" from "the plan was good and
    the coin came up tails". Without the ex-ante numbers, a batch of thirty
    cases cannot tell those apart.
    """

    read: bool
    acted: bool
    updated_instrument: bool
    reauthorised_mandate: bool
    accepted_concession: bool
    churned: bool
    read_probability_bps: int
    act_probability_bps: int
    concession_acceptance_bps: int
    churn_hazard_bps: int
    state: CustomerState


class CustomerModel:
    """The generative model of customer behaviour for one seeded world.

    Stateless with respect to any individual customer: state is passed in and a
    new state comes back. All randomness is keyed by the message or event
    identity rather than drawn from a running stream, so evaluating the same
    outreach twice gives the same answer and evaluating outreach in a different
    order changes nothing.
    """

    __slots__ = ("_seed",)

    def __init__(self, seed: int) -> None:
        self._seed = seed

    @property
    def seed(self) -> int:
        return self._seed

    # ----------------------------------------------------------- probabilities

    def read_probability(
        self, traits: CustomerTraits, outreach: Outreach, state: CustomerState
    ) -> Decimal:
        """P(the customer opens and reads this message).

        Falls with each prior contact. This is the term that makes a
        seven-message dunning sequence worth less than a two-message one.
        """
        probability = traits.responsiveness * traits.affinity_for(outreach.channel)
        if outreach.language != traits.language:
            probability *= WRONG_LANGUAGE_FACTOR
        probability *= self._fatigue(state.contacts_made)
        if self._is_quiet_hour(outreach.at):
            probability *= QUIET_HOUR_FACTOR
        return clamp_unit(probability)

    def act_probability(
        self, traits: CustomerTraits, outreach: Outreach, state: CustomerState
    ) -> Decimal:
        """P(the customer does the thing the message asked for | they read it).

        Intent dominates. A customer who has decided to leave reads the message
        and does nothing, which is why outreach volume cannot substitute for
        outreach relevance.
        """
        probability = traits.intent_to_pay
        if not outreach.addresses_true_cause:
            probability *= IRRELEVANT_MESSAGE_FACTOR
        probability *= _PURPOSE_FRICTION.get(outreach.purpose, ONE)
        if outreach.concession is not None and outreach.concession.is_positive:
            probability *= ONE + self.concession_acceptance(traits, outreach.concession, outreach.mrr)
        return clamp_unit(probability)

    def concession_acceptance(
        self, traits: CustomerTraits, concession: Money, mrr: Money
    ) -> Decimal:
        """P(a concession of this size persuades this customer).

        Meaningful relative to their own monthly spend, with diminishing
        returns: doubling a discount does not double its effect, so the planner
        is rewarded for finding the smallest concession that clears the bar
        rather than the largest the budget allows.
        """
        if not concession.is_positive or not mrr.is_positive:
            return ZERO
        ratio = (Decimal(concession.minor) / Decimal(mrr.minor)) * traits.price_sensitivity
        saturating = ratio / (ratio + CONCESSION_HALF_SATURATION)
        ceiling = CONCESSION_FLOOR_ACCEPTANCE + CONCESSION_INTENT_WEIGHT * traits.intent_to_pay
        return clamp_unit(ceiling * saturating)

    def churn_hazard(self, traits: CustomerTraits, state: CustomerState) -> Decimal:
        """P(the customer gives up on the subscription at this point).

        Rises with every contact and with every failed attempt, and rises faster
        for customers whose intent was already weak. A system that contacts a
        wavering customer five times to recover ninety-nine rupees has, on this
        model, a very good chance of losing the whole mandate -- which is the
        cost that frequency caps exist to avoid.
        """
        pressure = ONE + CHURN_PER_CONTACT * Decimal(state.contacts_made)
        pressure *= ONE + CHURN_PER_FAILED_ATTEMPT * Decimal(state.failed_attempts)
        reluctance = CHURN_INTENT_SPAN - traits.intent_to_pay
        hazard = traits.baseline_churn * pressure * reluctance
        return min(MAX_CHURN_HAZARD, clamp_unit(hazard))

    # ----------------------------------------------------------------- actions

    def respond(
        self, traits: CustomerTraits, outreach: Outreach, state: CustomerState
    ) -> CustomerResponse:
        """Deliver one message and return what happened plus the new state.

        The order of draws matters and is fixed: read, act, then the specific
        follow-through the purpose asks for, then churn. Churn is evaluated last
        because a customer who converts on this message has, by construction,
        not walked away from it.
        """
        rng = substream(self._seed, "outreach", traits.customer_id, outreach.message_key)

        read_p = self.read_probability(traits, outreach, state)
        read = bernoulli(rng, read_p)

        act_p = self.act_probability(traits, outreach, state)
        acted = read and bernoulli(rng, act_p)

        accept_p = (
            self.concession_acceptance(traits, outreach.concession, outreach.mrr)
            if outreach.concession is not None
            else ZERO
        )
        accepted = acted and outreach.concession is not None and bernoulli(rng, accept_p)

        updated_instrument = (
            acted
            and outreach.purpose is MessagePurpose.INSTRUMENT_UPDATE_REQUEST
            and bernoulli(rng, traits.digital_confidence)
        )
        reauthorised = (
            acted
            and outreach.purpose is MessagePurpose.MANDATE_REAUTHORISATION
            and bernoulli(rng, traits.digital_confidence * Decimal("0.80"))
        )

        next_state = state.after_contact(was_read=read)
        if accepted:
            next_state = replace(
                next_state,
                concessions_offered=next_state.concessions_offered + 1,
                concessions_accepted=next_state.concessions_accepted + 1,
            )
        elif outreach.concession is not None:
            next_state = replace(
                next_state, concessions_offered=next_state.concessions_offered + 1
            )
        if updated_instrument:
            next_state = replace(next_state, instrument_updated=True)
        if reauthorised:
            next_state = replace(next_state, mandate_reauthorised=True)

        churn_p = self.churn_hazard(traits, next_state)
        churned = (not acted) and (not state.churned) and bernoulli(rng, churn_p)
        if churned:
            next_state = replace(next_state, churned=True)

        return CustomerResponse(
            read=read,
            acted=acted,
            updated_instrument=updated_instrument,
            reauthorised_mandate=reauthorised,
            accepted_concession=accepted,
            churned=churned,
            read_probability_bps=to_bps(read_p),
            act_probability_bps=to_bps(act_p),
            concession_acceptance_bps=to_bps(accept_p),
            churn_hazard_bps=to_bps(churn_p),
            state=next_state,
        )

    def churn_after_failure(
        self, traits: CustomerTraits, state: CustomerState, *, event_key: str
    ) -> CustomerState:
        """Apply churn pressure from a failed debit the customer noticed.

        A declined mandate is visible to the customer -- their bank tells them --
        so it carries its own churn risk even when the merchant says nothing.
        """
        next_state = state.after_failed_attempt()
        if state.churned:
            return next_state
        rng = substream(self._seed, "failure-churn", traits.customer_id, event_key)
        if bernoulli(rng, self.churn_hazard(traits, next_state)):
            return replace(next_state, churned=True)
        return next_state

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _fatigue(contacts_made: int) -> Decimal:
        return ONE / (ONE + CONTACT_FATIGUE_K * Decimal(max(0, contacts_made)))

    @staticmethod
    def _is_quiet_hour(at: dt.datetime) -> bool:
        hour = ist_hour(at)
        return hour >= QUIET_HOURS_START_IST or hour < QUIET_HOURS_END_IST


#: Some asks are simply harder than others once the customer has agreed in
#: principle. Re-authorising a mandate means leaving the app and approving in a
#: bank interface; reading a reminder means nothing at all.
_PURPOSE_FRICTION: dict[MessagePurpose, Decimal] = {
    MessagePurpose.PAYMENT_FAILURE_NOTICE: Decimal("0.85"),
    MessagePurpose.PAYMENT_RECOVERY_OUTREACH: Decimal("0.90"),
    MessagePurpose.INSTRUMENT_UPDATE_REQUEST: Decimal("0.70"),
    MessagePurpose.MANDATE_REAUTHORISATION: Decimal("0.55"),
    MessagePurpose.STEP_UP_AUTHENTICATION: Decimal("0.80"),
    MessagePurpose.PROMOTIONAL_WINBACK: Decimal("0.45"),
}
