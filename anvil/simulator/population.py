"""Generating a plausible Indian subscription book.

Everything here is a pure function of the seed. The same seed produces the same
merchants, the same customers with the same latent traits, and the same
mandates -- which is what lets the batch report claim reproducibility literally
rather than aspirationally.

The distributions are chosen to be *defensible*, not flattering. Ticket sizes
are mostly small with a long tail, because that is what Indian subscription
commerce looks like; tenure skews young, because a book that has been growing
has more new customers than old ones; and a minority of mandates carry delegated
agent authority or a Reserve Pay block, because those are the interesting cases
and a population with none of them would never exercise the step-up path.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from anvil.core.ids import IdPrefix, deterministic_id
from anvil.domain.enums import AuthorisationStatus, AuthorisationType, Channel
from anvil.domain.money import Money
from anvil.simulator.customer import CustomerTraits
from anvil.simulator.issuer import Bank, Issuer
from anvil.simulator.rng import (
    bernoulli,
    skewed_int,
    substream,
    uniform_between,
    weighted_choice,
)

#: Monthly price points, with weights. A long tail above ₹999 and a heavy
#: concentration in the ₹99-₹499 band, which is where Indian consumer
#: subscriptions actually cluster.
PRICE_POINTS: tuple[tuple[int, int], ...] = (
    (99_00, 22),
    (149_00, 18),
    (199_00, 16),
    (299_00, 14),
    (399_00, 9),
    (499_00, 8),
    (799_00, 5),
    (999_00, 4),
    (1_499_00, 2),
    (2_999_00, 1),
    (4_999_00, 1),
)

LANGUAGES: tuple[tuple[str, int], ...] = (
    ("en", 42),
    ("hi", 26),
    ("ta", 7),
    ("te", 6),
    ("mr", 6),
    ("bn", 5),
    ("kn", 4),
    ("gu", 4),
)

AUTH_TYPES: tuple[tuple[AuthorisationType, int], ...] = (
    (AuthorisationType.UPI_AUTOPAY, 54),
    (AuthorisationType.ENACH, 24),
    (AuthorisationType.CARD_MANDATE, 14),
    (AuthorisationType.RESERVE_PAY, 5),
    (AuthorisationType.DELEGATED_AGENT, 3),
)


@dataclass(frozen=True, slots=True)
class SimPlan:
    plan_id: str
    name: str
    family: str
    tier_rank: int
    amount: Money


@dataclass(frozen=True, slots=True)
class SimAuthorisation:
    """A mandate as the simulator holds it, before it reaches the database."""

    authorisation_id: str
    auth_type: AuthorisationType
    status: AuthorisationStatus
    external_reference: str
    max_amount: Money
    valid_from: dt.datetime
    valid_until: dt.datetime | None
    max_attempts_per_cycle: int
    blocked_amount: Money | None = None
    delegated_to_agent: str | None = None
    agent_per_txn_cap: Money | None = None

    @property
    def is_delegated(self) -> bool:
        return self.delegated_to_agent is not None


@dataclass(frozen=True, slots=True)
class SimCustomer:
    """A customer, their subscription, their bank and their latent truth."""

    customer_id: str
    subscription_id: str
    display_name: str
    traits: CustomerTraits
    bank: Bank
    plan: SimPlan
    amount: Money
    tenure_days: int
    lifetime_value: Money
    authorisation: SimAuthorisation
    #: Ground truth the agent never sees: the instrument's real expiry.
    instrument_expires_at: dt.datetime | None
    #: Ground truth: this customer will revoke if pushed hard enough.
    revokes_under_pressure: bool


@dataclass(frozen=True, slots=True)
class Population:
    merchant_id: str
    merchant_name: str
    seed: int
    plans: tuple[SimPlan, ...]
    customers: tuple[SimCustomer, ...]
    generated_at: dt.datetime

    @property
    def total_mrr(self) -> Money:
        total = Money.zero()
        for c in self.customers:
            total = total + c.amount
        return total

    def fingerprint(self) -> str:
        """A stable digest of the whole population.

        Two runs with the same seed must produce the same fingerprint. The test
        that asserts this is what makes "reproducible on any machine" checkable
        rather than merely claimed.
        """
        import hashlib

        parts: list[str] = [self.merchant_id, str(self.seed)]
        for c in self.customers:
            parts.append(
                f"{c.customer_id}|{c.amount.minor}|{c.bank.code}|{c.tenure_days}"
                f"|{c.authorisation.auth_type.value}|{c.traits.intent_to_pay}"
                f"|{c.traits.ability_to_pay}"
            )
        return hashlib.blake2b("\x1e".join(parts).encode(), digest_size=16).hexdigest()


#: Realistic Indian given and family names, so nothing in a demo reads as
#: "John Doe". These are common names, used only to make a console legible.
_GIVEN = (
    "Aarav",
    "Diya",
    "Vihaan",
    "Ananya",
    "Advait",
    "Ishita",
    "Kabir",
    "Meera",
    "Rohan",
    "Saanvi",
    "Arjun",
    "Kavya",
    "Nikhil",
    "Priya",
    "Farhan",
    "Zoya",
    "Aditya",
    "Sneha",
    "Rahul",
    "Tara",
    "Imran",
    "Neha",
    "Karthik",
    "Divya",
)
_FAMILY = (
    "Sharma",
    "Iyer",
    "Patel",
    "Reddy",
    "Nair",
    "Banerjee",
    "Khan",
    "Menon",
    "Gupta",
    "Desai",
    "Rao",
    "Chatterjee",
    "Joshi",
    "Pillai",
    "Verma",
    "Shah",
)

_PLAN_FAMILIES = (
    ("Basic", 0),
    ("Standard", 1),
    ("Plus", 2),
    ("Pro", 3),
)


def build_plans(seed: int, merchant_id: str) -> tuple[SimPlan, ...]:
    """A ladder of plans, so a downgrade always has somewhere to go."""
    rng = substream(seed, "population", "plans", merchant_id)
    base = weighted_choice(rng, list(PRICE_POINTS))
    plans: list[SimPlan] = []
    for name, rank in _PLAN_FAMILIES:
        amount = Money(int(base * (Decimal("1.0") + Decimal(rank) * Decimal("0.85"))))
        plans.append(
            SimPlan(
                plan_id=deterministic_id("pln", merchant_id, name),
                name=name,
                family="core",
                tier_rank=rank,
                amount=amount,
            )
        )
    return tuple(plans)


def build_customer(
    seed: int, merchant_id: str, index: int, plans: tuple[SimPlan, ...], now: dt.datetime
) -> SimCustomer:
    """One customer, deterministically derived from the seed and their index."""
    rng = substream(seed, "population", "customer", str(index))
    customer_id = deterministic_id(IdPrefix.CUSTOMER, merchant_id, str(index))

    given = _GIVEN[rng.randrange(len(_GIVEN))]
    family = _FAMILY[rng.randrange(len(_FAMILY))]
    language = weighted_choice(rng, list(LANGUAGES))

    plan = plans[skewed_int(rng, 0, len(plans) - 1, skew=2)]

    # Tenure skews young: a growing book has more new customers than old ones.
    tenure_days = skewed_int(rng, 5, 1400, skew=3)

    # Latent truth. Intent and ability are correlated but distinct -- the whole
    # point of the diagnosis task is telling "cannot pay" from "will not pay".
    # Most subscribers can comfortably afford what they subscribed to -- a book
    # where a third of customers cannot pay is a book with a sales problem, not
    # a recovery problem. A minority genuinely live close to the line, and they
    # are the ones the salary cycle governs.
    if bernoulli(rng, Decimal("0.16")):
        ability = uniform_between(rng, Decimal("0.15"), Decimal("0.45"))
    else:
        ability = uniform_between(rng, Decimal("0.55"), Decimal("0.99"))
    intent = uniform_between(rng, max(Decimal("0.10"), ability - Decimal("0.35")), Decimal("0.99"))

    traits = CustomerTraits(
        customer_id=customer_id,
        language=language,
        intent_to_pay=intent,
        ability_to_pay=ability,
        responsiveness=uniform_between(rng, Decimal("0.20"), Decimal("0.85")),
        digital_confidence=uniform_between(rng, Decimal("0.30"), Decimal("0.97")),
        price_sensitivity=uniform_between(rng, Decimal("0.20"), Decimal("0.90")),
        baseline_churn=uniform_between(rng, Decimal("0.005"), Decimal("0.06")),
        channel_affinity=(
            (Channel.WHATSAPP, uniform_between(rng, Decimal("0.45"), Decimal("0.95"))),
            (Channel.SMS, uniform_between(rng, Decimal("0.25"), Decimal("0.70"))),
            (Channel.EMAIL, uniform_between(rng, Decimal("0.15"), Decimal("0.75"))),
            (Channel.IN_APP, uniform_between(rng, Decimal("0.20"), Decimal("0.65"))),
        ),
    )

    auth_type = weighted_choice(rng, list(AUTH_TYPES))
    authorisation = _build_authorisation(rng, seed, customer_id, plan.amount, auth_type, now)

    # Cards expire; UPI mandates mostly do not. A minority are already close.
    instrument_expires_at = None
    if auth_type is AuthorisationType.CARD_MANDATE:
        instrument_expires_at = now + dt.timedelta(days=skewed_int(rng, 10, 900, skew=2))

    return SimCustomer(
        customer_id=customer_id,
        subscription_id=deterministic_id(IdPrefix.SUBSCRIPTION, merchant_id, str(index)),
        display_name=f"{given} {family}",
        traits=traits,
        bank=Issuer(seed).pick_bank(customer_id),
        plan=plan,
        amount=plan.amount,
        tenure_days=tenure_days,
        lifetime_value=Money(plan.amount.minor * max(1, tenure_days // 30)),
        authorisation=authorisation,
        instrument_expires_at=instrument_expires_at,
        # Roughly one customer in eight will walk rather than be chased.
        revokes_under_pressure=bernoulli(rng, Decimal("0.12")),
    )


def _build_authorisation(
    rng,  # type: ignore[no-untyped-def]
    seed: int,
    customer_id: str,
    amount: Money,
    auth_type: AuthorisationType,
    now: dt.datetime,
) -> SimAuthorisation:
    """A mandate sized sensibly against the subscription it backs.

    The per-debit ceiling is set above the subscription amount, as real mandates
    are, so an ordinary debit is comfortably inside it and only the unusual
    cases -- a catch-up charge, a delegated agent with a tight cap -- come close
    to the boundary. A population where every debit sat at the limit would make
    the authorisation check look far more active than it is in production.
    """
    headroom = uniform_between(rng, Decimal("1.2"), Decimal("3.0"))
    max_amount = Money(int(Decimal(amount.minor) * headroom))

    blocked = None
    delegated_agent = None
    agent_cap = None

    if auth_type is AuthorisationType.RESERVE_PAY:
        # A block sized for a few cycles, so partial consumption is visible.
        blocked = Money(amount.minor * skewed_int(rng, 2, 6, skew=1))
    elif auth_type is AuthorisationType.DELEGATED_AGENT:
        delegated_agent = "agent:anvil"
        # Deliberately tight for some, so the AFA step-up path is exercised.
        agent_cap = Money(
            int(Decimal(amount.minor) * uniform_between(rng, Decimal("0.6"), Decimal("1.8")))
        )

    return SimAuthorisation(
        authorisation_id=deterministic_id(IdPrefix.AUTHORISATION, customer_id),
        auth_type=auth_type,
        status=AuthorisationStatus.ACTIVE,
        external_reference=f"UMN{deterministic_id('x', customer_id)[2:18]}",
        max_amount=max_amount,
        valid_from=now - dt.timedelta(days=skewed_int(rng, 30, 800, skew=2)),
        valid_until=now + dt.timedelta(days=skewed_int(rng, 40, 1200, skew=2)),
        max_attempts_per_cycle=weighted_choice(rng, [(3, 60), (4, 30), (2, 10)]),
        blocked_amount=blocked,
        delegated_to_agent=delegated_agent,
        agent_per_txn_cap=agent_cap,
    )


def build_population(
    *, seed: int, size: int, now: dt.datetime, merchant_name: str = "Kettle & Co"
) -> Population:
    """The whole book. Pure, deterministic, and free of any I/O."""
    if size < 1:
        raise ValueError("a population needs at least one customer")
    merchant_id = deterministic_id(IdPrefix.MERCHANT, merchant_name, str(seed))
    plans = build_plans(seed, merchant_id)
    customers = tuple(build_customer(seed, merchant_id, i, plans, now) for i in range(size))
    return Population(
        merchant_id=merchant_id,
        merchant_name=merchant_name,
        seed=seed,
        plans=plans,
        customers=customers,
        generated_at=now,
    )
