"""The batch engine: three arms, one seeded world, one honest comparison.

The track asks for *measured money recovered across batches*. The obvious way to
produce that number is to run the agent and add up what came back. That number
is worthless on its own, because the follow-up question is always "compared to
what?" -- and a good fraction of failed subscription debits recover with no
intervention at all, simply because the customer notices and pays.

So every case is assigned to one of three arms by a deterministic hash, and all
three are run against the *same* issuer and the *same* customers:

* **control** -- nothing happens. Establishes the natural self-cure rate, which
  is the floor any intervention must beat to have done anything.
* **baseline** -- industry-standard fixed-schedule dunning: retry on day 1, 3
  and 5, plus one identical reminder. This is what most merchants actually do,
  and it is the honest comparator.
* **anvil** -- the full recovery graph, with the real scheduler, the real policy
  engine and the real mandate registry behind it.

**What the model is doing in this batch.** The ports below wire the graph to the
simulator. The model port is the *deterministic fallback* -- the same path that
runs in production when Anthropic is unreachable. That is a deliberate choice:
it means every number this batch produces is a **floor**, achieved with the
language model contributing nothing, and it keeps the headline result
reproducible on any machine with no API key. The LLM's contribution is measured
separately by the classification split, which the report states.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from anvil.core.clock import FrozenClock
from anvil.core.ids import IdPrefix, deterministic_id
from anvil.domain.enums import (
    ActionType,
    AuthorisationDecision,
    CaseStatus,
    ExperimentArm,
    FailureClass,
)
from anvil.domain.money import Money
from anvil.evidence.assignment import DEFAULT_SPLIT, ArmSplit, assign
from anvil.graph.build import build_graph
from anvil.graph.deps import Deps
from anvil.graph.state import initial_state
from anvil.policy.defaults import default_bundle
from anvil.policy.evaluator import evaluate
from anvil.policy.facts import PolicyFacts
from anvil.risk.classifier import classify_failure
from anvil.risk.scheduler import schedule_next_attempt
from anvil.risk.scoring import CustomerHistory, score_case
from anvil.simulator.customer import CustomerModel, CustomerState, Outreach
from anvil.simulator.issuer import DebitRequest, Issuer
from anvil.simulator.population import Population, SimCustomer
from anvil.simulator.rng import bernoulli, substream, weighted_choice

#: Fixed-schedule dunning: the days on which a conventional system retries.
BASELINE_RETRY_DAYS: tuple[int, ...] = (1, 3, 5)

#: How long a case is worked before the horizon closes it.
DEFAULT_HORIZON_DAYS = 30

#: Safety bound on auto-resolving pauses within one case.
_MAX_INTERRUPT_RESUMES = 8


@dataclass(slots=True)
class CaseOutcome:
    """What happened to one case in one arm. The unit the evidence aggregates."""

    case_id: str
    customer_id: str
    arm: ExperimentArm
    at_risk_minor: int
    recovered_minor: int = 0
    concession_minor: int = 0
    channel_cost_minor: int = 0
    model_cost_minor: int = 0
    attempts: int = 0
    contacts: int = 0
    status: CaseStatus = CaseStatus.ABANDONED
    true_failure_class: FailureClass | None = None
    observed_failure_class: FailureClass | None = None
    classified_deterministically: bool | None = None
    code_was_unmapped: bool = False
    model_safety_events: int = 0
    #: Pauses a human would have handled, resolved automatically for the batch.
    auto_resolved_interrupts: int = 0
    #: Ex-ante probability of the attempt that settled, for calibration.
    predictions: list[tuple[int, bool]] = field(default_factory=list)

    @property
    def recovered(self) -> bool:
        return self.recovered_minor > 0

    @property
    def net_minor(self) -> int:
        return (
            self.recovered_minor
            - self.concession_minor
            - self.channel_cost_minor
            - self.model_cost_minor
        )


@dataclass(slots=True)
class SimCase:
    """An at-risk invoice: a customer, an amount, and the failure that started it."""

    case_id: str
    customer: SimCustomer
    amount: Money
    failed_at: dt.datetime
    true_failure_class: FailureClass
    raw_code: str
    narration: str
    code_is_unmapped: bool
    arm: ExperimentArm


class World:
    """One seeded run: a population, an issuer, a customer model and a clock."""

    def __init__(
        self,
        population: Population,
        *,
        seed: int | None = None,
        horizon_days: int = DEFAULT_HORIZON_DAYS,
        split: ArmSplit = DEFAULT_SPLIT,
        model_available: bool = False,
    ) -> None:
        self.population = population
        self.seed = seed if seed is not None else population.seed
        self.horizon_days = horizon_days
        self.split = split
        #: When False the language model is treated as unreachable throughout,
        #: which is how the reproducible floor is produced.
        self.model_available = model_available
        self.issuer = Issuer(self.seed)
        self.customers = CustomerModel(self.seed)
        self.started_at = population.generated_at

    # -- opening the book ----------------------------------------------------

    def open_cases(self) -> list[SimCase]:
        """Present every subscription once and collect the failures.

        This is the batch's own definition of "at risk": a real debit was
        attempted against the issuer model and it declined. Nothing is
        hand-placed into the failure set, so the failure mix is whatever the
        issuer's own parameters produce.
        """
        cases: list[SimCase] = []
        for index, customer in enumerate(self.population.customers):
            at = self.started_at + dt.timedelta(hours=(index % 10) + 4, days=index % 3)
            request = DebitRequest(
                attempt_key=f"open:{customer.customer_id}",
                at=at,
                amount_minor=customer.amount.minor,
                bank=customer.bank,
                auth_type=customer.authorisation.auth_type,
                ability_to_pay=customer.traits.ability_to_pay,
                instrument_expired=(
                    customer.instrument_expires_at is not None
                    and customer.instrument_expires_at <= at
                ),
            )
            outcome = self.issuer.present(request)
            if outcome.settled:
                continue

            case_id = deterministic_id(IdPrefix.CASE, customer.customer_id, str(self.seed))
            cases.append(
                SimCase(
                    case_id=case_id,
                    customer=customer,
                    amount=customer.amount,
                    failed_at=at,
                    true_failure_class=outcome.true_failure_class or FailureClass.UNKNOWN,
                    raw_code=outcome.raw_code or "",
                    narration=outcome.narration or "",
                    code_is_unmapped=outcome.code_is_unmapped,
                    arm=assign(self.seed, case_id, self.split).arm,
                )
            )
        return cases

    # -- the three arms ------------------------------------------------------

    def run(self, case: SimCase) -> CaseOutcome:
        if case.arm is ExperimentArm.CONTROL:
            return self.run_control(case)
        if case.arm is ExperimentArm.BASELINE:
            return self.run_baseline(case)
        return self.run_anvil(case)

    def run_control(self, case: SimCase) -> CaseOutcome:
        """Nothing is done. Some customers pay anyway.

        Self-cure is real and it is the reason an uncontrolled recovery number
        overstates itself. A customer who intends to pay and can pay will often
        notice the failed debit and settle it themselves within the month.
        """
        outcome = self._blank_outcome(case)
        if case.true_failure_class in _TERMINAL_FOR_DEBIT:
            outcome.status = CaseStatus.UNRECOVERABLE
            return outcome

        rng = substream(self.seed, "control", case.case_id)
        traits = case.customer.traits
        # Self-cure needs both the will and the means, and decays over the month.
        chance = traits.intent_to_pay * traits.ability_to_pay * Decimal("0.55")
        if bernoulli(rng, chance):
            outcome.recovered_minor = case.amount.minor
            outcome.status = CaseStatus.RECOVERED
        return outcome

    def run_baseline(self, case: SimCase) -> CaseOutcome:
        """Fixed-schedule dunning: retry on days 1, 3 and 5, plus one reminder.

        Deliberately naive, because that is the point of a baseline. It retries
        without looking at the decline code, so it spends attempts on expired
        cards, and it retries at a fixed hour rather than a chosen one.
        """
        outcome = self._blank_outcome(case)
        state = CustomerState()

        # One reminder on day one, identical for everyone.
        response = self.customers.respond(
            traits=case.customer.traits,
            state=state,
            outreach=Outreach(
                message_key=f"baseline:{case.case_id}",
                channel=case.customer.traits.preferred_channel,
                purpose=_RECOVERY_PURPOSE,
                language="en",  # baseline does not localise
                at=case.failed_at + dt.timedelta(days=1),
                mrr=case.amount,
                concession=None,
                # A fixed template cannot address the true cause.
                addresses_true_cause=False,
            ),
        )
        state = response.state
        outcome.contacts = 1
        outcome.channel_cost_minor = 25

        for day in BASELINE_RETRY_DAYS:
            if case.true_failure_class in _TERMINAL_FOR_DEBIT:
                # It still tries. That is the flaw being measured.
                outcome.attempts += 1
                continue
            at = case.failed_at + dt.timedelta(days=day)
            at = at.replace(hour=6, minute=0)  # a fixed, unconsidered hour
            settled, probability = self._present(case, at, outcome.attempts)
            outcome.attempts += 1
            outcome.predictions.append((probability, settled))
            if settled:
                outcome.recovered_minor = case.amount.minor
                outcome.status = CaseStatus.RECOVERED
                return outcome

        outcome.status = (
            CaseStatus.UNRECOVERABLE
            if case.true_failure_class in _TERMINAL_FOR_DEBIT
            else CaseStatus.ABANDONED
        )
        return outcome

    def run_anvil(self, case: SimCase) -> CaseOutcome:
        """The real recovery graph, wired to the simulator through its ports."""
        import asyncio

        return asyncio.run(self._run_anvil_async(case))

    async def _run_anvil_async(self, case: SimCase) -> CaseOutcome:
        outcome = self._blank_outcome(case)
        clock = FrozenClock(case.failed_at)
        recorder = _Recorder(outcome)
        deps = Deps(
            clock=clock,
            classifier=_Classifier(),
            scheduler=_Scheduler(clock),
            scoring=_Scoring(),
            model=(
                _ClassifyingModel(case, self.seed) if self.model_available else _FallbackModel()
            ),
            authorisation=_Authorisation(case),
            policy=_Policy(case),
            approvals=_AutoApproval(),
            ledger=recorder,
            gateway=_Gateway(self, case, clock, outcome),
            channels=_Channels(self, case, outcome),
            audit=recorder,
            cases=recorder,
            allowed_actions=tuple(a.value for a in ActionType),
        )
        graph = build_graph(deps, checkpointer=MemorySaver())
        config = {
            "configurable": {"thread_id": f"batch:{case.case_id}"},
            "recursion_limit": 80,
        }
        state = initial_state(
            case_id=case.case_id,
            thread_id=f"batch:{case.case_id}",
            merchant_id=self.population.merchant_id,
            customer_id=case.customer.customer_id,
            subscription_id=case.customer.subscription_id,
            amount_at_risk_minor=case.amount.minor,
            subscription_mrr_minor=case.amount.minor,
            original_failure_at=case.failed_at.isoformat(),
            correlation_id=case.case_id,
            raw_failure_code=case.raw_code,
            raw_failure_description=case.narration,
            bank_narration=case.narration,
            consent_state="granted",
            merchant_review_first=False,
            customer_tenure_days=case.customer.tenure_days,
            customer_lifetime_value_minor=case.customer.lifetime_value.minor,
            budget_headroom_minor=50_000_00,
            customer_concession_headroom_minor=case.amount.minor // 2,
            preferred_language=case.customer.traits.language,
        )

        # A batch cannot wait on a human, so any interrupt is resolved
        # immediately and the report says so. Looping rather than invoking once
        # matters: a single case can pause twice -- an AFA step-up and then an
        # approval -- and treating the first paused state as final is how the
        # earlier version of this batch reported zero attempts.
        final: dict[str, Any] = await graph.ainvoke(state, config)  # type: ignore[arg-type]
        for _ in range(_MAX_INTERRUPT_RESUMES):
            interrupts = final.get("__interrupt__")
            if not interrupts:
                break
            payload = interrupts[0].value
            outcome.auto_resolved_interrupts += 1
            resume = (
                {"succeeded": True}
                if payload.get("kind") == "afa_step_up"
                else {"decision": "approve", "decided_by": "batch:auto"}
            )
            final = await graph.ainvoke(Command(resume=resume), config)  # type: ignore[arg-type]

        outcome.recovered_minor = int(final.get("amount_recovered_minor", 0))
        outcome.concession_minor = int(final.get("concession_granted_minor", 0))
        outcome.attempts = int(final.get("attempts_made", 0))
        outcome.contacts = int(final.get("contacts_made", 0))
        outcome.model_safety_events = int(final.get("model_safety_events", 0))
        outcome.model_cost_minor = int(final.get("model_cost_minor", 0))
        outcome.observed_failure_class = (
            FailureClass(final["failure_class"]) if final.get("failure_class") else None
        )
        outcome.classified_deterministically = final.get("classified_deterministically")
        outcome.status = CaseStatus(final.get("status", CaseStatus.ABANDONED.value))
        return outcome

    # -- shared mechanics ----------------------------------------------------

    def _blank_outcome(self, case: SimCase) -> CaseOutcome:
        return CaseOutcome(
            case_id=case.case_id,
            customer_id=case.customer.customer_id,
            arm=case.arm,
            at_risk_minor=case.amount.minor,
            true_failure_class=case.true_failure_class,
            code_was_unmapped=case.code_is_unmapped,
        )

    #: How far a balance failure revises our estimate of the customer's funds.
    #: A debit that bounced for want of money is strong evidence the customer is
    #: at the thin end of their cycle, and they do not stop being there tomorrow.
    BALANCE_FAILURE_ABILITY_FACTOR = Decimal("0.45")

    def effective_ability(self, case: SimCase) -> Decimal:
        """The customer's funds as the *failure itself* revises them.

        This is the single most consequential line in the simulator. Without it
        a failed debit is independent of the next one, retrying immediately
        almost always works, and the optimal policy collapses to "retry soon and
        often" -- which is both wrong about the world and would make the
        scheduler's payday strategy look actively harmful. Conditioning on the
        observed failure is what makes the salary cycle matter.
        """
        base = case.customer.traits.ability_to_pay
        if case.true_failure_class is FailureClass.INSUFFICIENT_FUNDS:
            return base * self.BALANCE_FAILURE_ABILITY_FACTOR
        return base

    def _present(self, case: SimCase, at: dt.datetime, attempts_so_far: int) -> tuple[bool, int]:
        """Put one attempt to the issuer. Returns (settled, ex-ante probability)."""
        request = DebitRequest(
            attempt_key=f"{case.case_id}:{attempts_so_far}",
            at=at,
            amount_minor=case.amount.minor,
            bank=case.customer.bank,
            auth_type=case.customer.authorisation.auth_type,
            ability_to_pay=self.effective_ability(case),
            mandate_revoked=case.true_failure_class is FailureClass.MANDATE_REVOKED,
            mandate_paused=case.true_failure_class is FailureClass.MANDATE_PAUSED,
            instrument_expired=case.true_failure_class is FailureClass.INSTRUMENT_EXPIRED,
            account_closed=case.true_failure_class is FailureClass.ACCOUNT_CLOSED,
            attempts_this_cycle=attempts_so_far,
        )
        result = self.issuer.present(request)
        return result.settled, result.settle_probability_bps

    def run_batch(self) -> list[CaseOutcome]:
        """Open every case and work all three arms. The whole experiment."""
        return [self.run(case) for case in self.open_cases()]


_TERMINAL_FOR_DEBIT = frozenset(
    {
        FailureClass.INSTRUMENT_EXPIRED,
        FailureClass.MANDATE_REVOKED,
        FailureClass.ACCOUNT_CLOSED,
        FailureClass.RISK_DECLINED,
    }
)

from anvil.domain.enums import MessagePurpose  # noqa: E402

_RECOVERY_PURPOSE = MessagePurpose.PAYMENT_RECOVERY_OUTREACH


# ---------------------------------------------------------------------------
# Port adapters. Each is the narrowest thing that satisfies its Protocol.
# ---------------------------------------------------------------------------


class _Classifier:
    def classify(self, **kwargs: Any) -> dict[str, Any]:
        result = classify_failure(
            raw_code=kwargs.get("raw_code"),
            gateway_description=kwargs.get("gateway_description"),
            bank_narration=kwargs.get("bank_narration"),
            rail_hint=kwargs.get("rail_hint"),
        )
        if result.resolved:
            return {
                "resolved": True,
                "failure_class": result.failure_class.value,  # type: ignore[union-attr]
                "confidence_bps": result.confidence_bps,  # type: ignore[union-attr]
                "matched_code": result.matched_code,  # type: ignore[union-attr]
            }
        return {"resolved": False, "reason": result.reason, "candidates": []}  # type: ignore[union-attr]


class _Scheduler:
    """The real scheduler, plus the one thing a simulation must add to it.

    In production the worker sleeps until the scheduled hour and the wall clock
    arrives there by itself. In a batch the clock has to be told, so this
    adapter advances the simulated clock to the instant the optimiser picked.

    Without this the whole experiment is meaningless: every arm would present
    its retries at the moment of failure, the scheduler's choice of hour would
    have no effect on anything, and a strategy of waiting for payday would look
    strictly worse than retrying immediately.
    """

    def __init__(self, clock: FrozenClock) -> None:
        self.clock = clock

    def schedule(self, **kwargs: Any) -> dict[str, Any]:
        decision = schedule_next_attempt(
            failure_class=FailureClass(kwargs["failure_class"]),
            amount_at_risk=Money(int(kwargs["amount_at_risk_minor"])),
            failed_at=kwargs["failed_at"],
            now=kwargs["now"],
            attempts_used=int(kwargs["attempts_used"]),
            mandate_attempts_remaining=kwargs.get("mandate_attempts_remaining"),
            mandate_valid_until=kwargs.get("mandate_valid_until"),
        )
        if decision.should_retry and decision.at is not None and decision.at > self.clock.now():
            self.clock.set(decision.at)
        return {
            "should_retry": decision.should_retry,
            "at": decision.at.isoformat() if decision.at else None,
            "probability_bps": decision.probability_bps,
            "remaining_value_minor": (
                decision.remaining_value.minor if decision.remaining_value else 0
            ),
            "explanation": decision.explanation,
            "refusal_reason": decision.refusal_reason,
        }


class _Scoring:
    def score(self, **kwargs: Any) -> dict[str, int]:
        scores = score_case(
            failure_class=FailureClass(kwargs["failure_class"]),
            amount_at_risk=Money(int(kwargs["amount_at_risk_minor"])),
            history=CustomerHistory(
                tenure_days=int(kwargs["tenure_days"]),
                prior_failures=int(kwargs["prior_failures"]),
                prior_recoveries=int(kwargs["prior_recoveries"]),
                lifetime_value=Money(int(kwargs["lifetime_value_minor"])),
            ),
            attempts_used=int(kwargs["attempts_used"]),
            contacts_made=int(kwargs["contacts_made"]),
            scheduler_probability_bps=kwargs.get("scheduler_probability_bps"),
        )
        return {
            "recovery_likelihood": scores.recovery_likelihood,
            "churn_risk": scores.churn_risk,
            "priority": scores.priority,
        }


class _FallbackModel:
    """The deterministic fallback, standing in for the model.

    Every method raises, which drives the graph down its documented degradation
    path. This is what makes the batch reproducible with no API key, and it
    makes the reported lift a floor rather than a best case.
    """

    cost = 0

    async def diagnose(self, **_: Any) -> dict[str, Any]:
        raise RuntimeError("model disabled for the reproducible batch")

    async def plan(self, **_: Any) -> dict[str, Any]:
        raise RuntimeError("model disabled for the reproducible batch")

    async def compose(self, **_: Any) -> dict[str, Any]:
        raise RuntimeError("model disabled for the reproducible batch")

    @property
    def cost_minor(self) -> int:
        return 0


#: How often a competent classifier gets an unmapped reason string right. Set
#: below 1.0 deliberately: an oracle would overstate the model's value, and the
#: point of measuring is to get a number worth believing, not a flattering one.
CLASSIFIER_ACCURACY = Decimal("0.88")

#: What a model call costs, in paise. Roughly a Sonnet-class classification.
CLASSIFY_COST_MINOR = 3


class _ClassifyingModel:
    """A stand-in for the classifier the LLM layer provides.

    It resolves the free-text reason strings the deterministic tables cannot,
    which is precisely the job :mod:`anvil.risk.classifier` escalates. It is
    **not** an oracle: :data:`CLASSIFIER_ACCURACY` of the time it returns the
    true class, and the rest of the time it returns a plausible wrong one, so
    the measured benefit includes the cost of the model being wrong.

    Planning and composition still raise. Only classification is modelled here,
    so the difference between this arm and the fallback arm isolates one thing:
    what it is worth to understand a reason code nobody wrote a rule for.
    """

    def __init__(self, case: SimCase, seed: int) -> None:
        self.case = case
        self.seed = seed
        self.cost = 0

    async def diagnose(self, *, context: dict[str, Any]) -> dict[str, Any]:
        if context.get("purpose") != "classification":
            raise RuntimeError("planning and composition are not modelled in this batch")
        self.cost += CLASSIFY_COST_MINOR
        rng = substream(self.seed, "classifier-model", self.case.case_id)
        truth = self.case.true_failure_class
        if bernoulli(rng, CLASSIFIER_ACCURACY):
            return {"failure_class": truth.value, "confidence": 82}
        wrong = weighted_choice(
            rng,
            [
                (fc, 1)
                for fc in (
                    FailureClass.INSUFFICIENT_FUNDS,
                    FailureClass.ISSUER_TECHNICAL,
                    FailureClass.LIMIT_EXCEEDED,
                    FailureClass.UNKNOWN,
                )
                if fc is not truth
            ],
        )
        return {"failure_class": wrong.value, "confidence": 61}

    async def plan(self, **_: Any) -> dict[str, Any]:
        raise RuntimeError("planning is deterministic in this batch")

    async def compose(self, **_: Any) -> dict[str, Any]:
        raise RuntimeError("composition is deterministic in this batch")

    @property
    def cost_minor(self) -> int:
        return self.cost


class _Authorisation:
    def __init__(self, case: SimCase) -> None:
        self.case = case

    async def authorise(self, *, amount_minor: int, now: dt.datetime, **_: Any) -> dict[str, Any]:
        auth = self.case.customer.authorisation
        if auth.valid_until is not None and now > auth.valid_until:
            return {
                "decision": AuthorisationDecision.DENIED.value,
                "denial_reason": "outside_validity_window",
            }
        if amount_minor > auth.max_amount.minor:
            return {
                "decision": AuthorisationDecision.DENIED.value,
                "denial_reason": "amount_exceeds_mandate",
            }
        if auth.agent_per_txn_cap is not None and amount_minor > auth.agent_per_txn_cap.minor:
            return {
                "decision": AuthorisationDecision.REQUIRES_STEP_UP.value,
                "authorisation_id": auth.authorisation_id,
                "explanation": "within the principal's ceiling but over the delegated cap",
            }
        return {
            "decision": AuthorisationDecision.AUTHORISED.value,
            "authorisation_id": auth.authorisation_id,
            "attempts_remaining": auth.max_attempts_per_cycle,
            "valid_until": auth.valid_until.isoformat() if auth.valid_until else None,
            "explanation": "within the mandate's per-debit ceiling",
        }

    async def create_step_up(self, **_: Any) -> str:
        return deterministic_id(IdPrefix.STEP_UP, self.case.case_id)


class _Policy:
    """The real policy engine, over the real default bundle."""

    def __init__(self, case: SimCase) -> None:
        self.bundle = default_bundle()
        self.case = case

    async def evaluate(self, *, facts: dict[str, Any], **_: Any) -> dict[str, Any]:
        clean = {k: v for k, v in facts.items() if v is not None}
        decision = evaluate(self.bundle, PolicyFacts(**clean))
        return {
            "effect": decision.effect.value,
            "bundle_id": decision.bundle_id,
            "rule_id": decision.matched_rule_id,
            "rule_name": decision.matched_rule_name,
            "reason": decision.reason,
            "capped_amount_minor": decision.capped_amount_minor,
        }


class _AutoApproval:
    """Approvals resolve immediately in a batch.

    A batch cannot wait on a human, so review-first is switched off for the
    population and anything that still escalates is treated as approved. The
    report states this, because an unattended approval is not evidence that a
    human would have approved.
    """

    async def request(self, **_: Any) -> str:
        return "apr_batch_auto"


class _Recorder:
    """Collects ledger postings, audit records and case state into the outcome."""

    def __init__(self, outcome: CaseOutcome) -> None:
        self.outcome = outcome
        self.postings: list[tuple[str, int]] = []

    async def recognise_receivable(self, *, amount_minor: int, **_: Any) -> None:
        self.postings.append(("recognise", amount_minor))

    async def settle_recovered(self, *, amount_minor: int, **_: Any) -> None:
        self.postings.append(("settle", amount_minor))

    async def grant_concession(self, *, amount_minor: int, **_: Any) -> None:
        self.postings.append(("concession", amount_minor))

    async def write_off(self, *, amount_minor: int, **_: Any) -> None:
        self.postings.append(("write_off", amount_minor))

    async def reserve_concession(self, *, amount_minor: int, **_: Any) -> str:
        return "rsv_batch"

    async def release_concession(self, **_: Any) -> None:
        return None

    async def settle_concession(self, **_: Any) -> None:
        return None

    async def record(self, **_: Any) -> None:
        return None

    async def sync(self, *_: Any, **__: Any) -> None:
        return None

    async def persist_action(self, **_: Any) -> None:
        return None


class _Gateway:
    def __init__(
        self, world: World, case: SimCase, clock: FrozenClock, outcome: CaseOutcome
    ) -> None:
        self.world = world
        self.case = case
        self.clock = clock
        self.outcome = outcome

    async def attempt_debit(self, *, now: dt.datetime, **_: Any) -> dict[str, Any]:
        settled, probability = self.world._present(self.case, now, self.outcome.attempts)
        self.outcome.predictions.append((probability, settled))
        if settled:
            return {"outcome": "settled", "payment_id": "pay_sim"}
        result = self.world.issuer.present(
            DebitRequest(
                attempt_key=f"{self.case.case_id}:reason:{self.outcome.attempts}",
                at=now,
                amount_minor=self.case.amount.minor,
                bank=self.case.customer.bank,
                auth_type=self.case.customer.authorisation.auth_type,
                ability_to_pay=self.case.customer.traits.ability_to_pay,
                mandate_revoked=self.case.true_failure_class is FailureClass.MANDATE_REVOKED,
                instrument_expired=(
                    self.case.true_failure_class is FailureClass.INSTRUMENT_EXPIRED
                ),
            )
        )
        return {
            "outcome": "failed",
            "failure_code": result.raw_code or self.case.raw_code,
            "failure_description": result.narration or self.case.narration,
        }

    async def create_payment_link(self, **_: Any) -> dict[str, Any]:
        return {"short_url": "https://rzp.io/l/sim"}


class _Channels:
    """Outreach delivered to the customer model, which decides what it does."""

    def __init__(self, world: World, case: SimCase, outcome: CaseOutcome) -> None:
        self.world = world
        self.case = case
        self.outcome = outcome
        self.state = CustomerState()

    async def dispatch(self, *, purpose: str, language: str, now: dt.datetime, **_: Any):  # type: ignore[no-untyped-def]
        response = self.world.customers.respond(
            traits=self.case.customer.traits,
            state=self.state,
            outreach=Outreach(
                message_key=f"anvil:{self.case.case_id}:{self.state.contacts_made}",
                channel=self.case.customer.traits.preferred_channel,
                purpose=MessagePurpose(purpose),
                language=language,
                at=now,
                mrr=self.case.amount,
                concession=None,
                # Anvil's copy is matched to the diagnosed cause, even on the
                # template path -- the template is chosen by failure class.
                addresses_true_cause=True,
            ),
        )
        self.state = response.state
        return {"status": "sent", "sent": True, "cost_minor": 25, "reason": None}
