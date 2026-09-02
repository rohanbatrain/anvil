"""What the recovery graph needs from the rest of Anvil, expressed as Protocols.

The graph is the one module that touches everything -- classification, scoring,
authorisation, policy, the ledger, channels, the gateway and the model. If it
imported all of them it would be untestable, unbuildable in parallel, and the
dependency arrows would point from the orchestrator into every leaf.

So it imports none of them. It declares the narrow slice of behaviour it needs
and lets the composition root in :mod:`anvil.graph.wiring` supply concrete
implementations. Three things follow, and all three are worth the indirection:

* Every node can be tested against a hand-written double in a few lines, with no
  database, no network and no model.
* A dependency that fails -- the model most of all -- is a stub that raises,
  which is exactly how the degradation paths get tested honestly.
* The slice each port exposes *is* the contract. Reading this file tells you
  precisely how much authority the orchestrator has over the ledger, which is a
  question worth being able to answer in one place.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ClassifierPort(Protocol):
    """Deterministic failure classification, with an explicit escalation result."""

    def classify(
        self,
        *,
        raw_code: str | None,
        gateway_description: str | None,
        bank_narration: str | None,
        rail_hint: str | None,
    ) -> dict[str, Any]:
        """Return ``{"resolved": bool, "failure_class": str|None, "confidence_bps": int, ...}``."""
        ...


@runtime_checkable
class SchedulerPort(Protocol):
    """The deterministic retry optimiser. Never a model."""

    def schedule(
        self,
        *,
        failure_class: str,
        amount_at_risk_minor: int,
        failed_at: dt.datetime,
        now: dt.datetime,
        attempts_used: int,
        mandate_attempts_remaining: int | None,
        mandate_valid_until: dt.datetime | None,
    ) -> dict[str, Any]:
        """Return ``{"should_retry": bool, "at": iso|None, "probability_bps": int, ...}``."""
        ...


@runtime_checkable
class ScoringPort(Protocol):
    def score(
        self,
        *,
        failure_class: str,
        amount_at_risk_minor: int,
        tenure_days: int,
        prior_failures: int,
        prior_recoveries: int,
        lifetime_value_minor: int,
        attempts_used: int,
        contacts_made: int,
        scheduler_probability_bps: int | None,
    ) -> dict[str, int]:
        """Return ``{"recovery_likelihood": int, "churn_risk": int, "priority": int}``."""
        ...


@runtime_checkable
class ModelPort(Protocol):
    """The language model. Every method may raise; the graph degrades when it does.

    Deliberately three narrow methods rather than one general one. A port that
    exposed "ask the model anything" would let a future node quietly hand the
    model a decision the architecture says it must not have.
    """

    async def diagnose(self, *, context: dict[str, Any]) -> dict[str, Any]: ...

    async def plan(
        self, *, context: dict[str, Any], allowed_actions: list[str], budget_minor: int
    ) -> dict[str, Any]: ...

    async def compose(
        self, *, context: dict[str, Any], purpose: str, language: str, allowed_facts: list[str]
    ) -> dict[str, Any]: ...

    @property
    def cost_minor(self) -> int:
        """Cumulative spend so far, so the case can carry its own model cost."""
        ...


@runtime_checkable
class AuthorisationPort(Protocol):
    """The mandate registry. Fails closed, always."""

    async def authorise(
        self,
        *,
        customer_id: str,
        subscription_id: str,
        action_type: str,
        amount_minor: int,
        now: dt.datetime,
    ) -> dict[str, Any]:
        """Return ``{"decision": "authorised"|"requires_step_up"|"denied", ...}``."""
        ...

    async def create_step_up(
        self,
        *,
        case_id: str,
        action_id: str,
        authorisation_id: str,
        customer_id: str,
        amount_minor: int,
        thread_id: str,
        now: dt.datetime,
    ) -> str:
        """Start an AFA challenge and return its id."""
        ...


@runtime_checkable
class PolicyPort(Protocol):
    """The deterministic policy engine. Evaluated per action, never by a model."""

    async def evaluate(
        self, *, case_id: str, merchant_id: str, facts: dict[str, Any]
    ) -> dict[str, Any]:
        """Return ``{"effect": str, "rule_id": str|None, "capped_amount_minor": int|None, ...}``."""
        ...


@runtime_checkable
class ApprovalPort(Protocol):
    """Creating the queue item a human resolves."""

    async def request(
        self,
        *,
        case_id: str,
        action: dict[str, Any],
        thread_id: str,
        merchant_id: str,
        escalation_reason: str,
        now: dt.datetime,
    ) -> str: ...


@runtime_checkable
class LedgerPort(Protocol):
    """The graph's authority over the books, stated in full.

    Note what is absent: there is no ``post`` and no way to construct an
    arbitrary entry. The orchestrator can record the four economic events a
    recovery can cause and nothing else, so a bug in a node cannot invent a
    posting the chart of accounts never anticipated.
    """

    async def recognise_receivable(
        self,
        *,
        case_id: str,
        customer_id: str,
        merchant_id: str,
        amount_minor: int,
        at: dt.datetime,
    ) -> None: ...

    async def settle_recovered(
        self,
        *,
        case_id: str,
        action_id: str,
        customer_id: str,
        merchant_id: str,
        amount_minor: int,
        at: dt.datetime,
    ) -> None: ...

    async def grant_concession(
        self,
        *,
        case_id: str,
        action_id: str,
        customer_id: str,
        merchant_id: str,
        amount_minor: int,
        at: dt.datetime,
    ) -> None: ...

    async def write_off(
        self,
        *,
        case_id: str,
        customer_id: str,
        merchant_id: str,
        amount_minor: int,
        reason: str,
        at: dt.datetime,
    ) -> None: ...

    async def reserve_concession(
        self,
        *,
        case_id: str,
        action_id: str,
        customer_id: str,
        merchant_id: str,
        amount_minor: int,
        subscription_mrr_minor: int,
        now: dt.datetime,
    ) -> str:
        """Take a hold and return its id, or raise BudgetExhausted."""
        ...

    async def release_concession(self, *, reservation_id: str, now: dt.datetime) -> None: ...

    async def settle_concession(self, *, reservation_id: str, now: dt.datetime) -> None: ...


@runtime_checkable
class GatewayPort(Protocol):
    """Money movement. Every call takes an idempotency key the caller owns."""

    async def attempt_debit(
        self,
        *,
        case_id: str,
        subscription_id: str,
        amount_minor: int,
        idempotency_key: str,
        now: dt.datetime,
    ) -> dict[str, Any]:
        """Return ``{"outcome": "settled"|"failed"|"unknown", ...}``.

        ``unknown`` is a first-class outcome, not an error. It means the request
        may or may not have moved money, and the only correct response is
        reconciliation with the same key.
        """
        ...

    async def create_payment_link(
        self, *, case_id: str, customer_id: str, amount_minor: int, idempotency_key: str
    ) -> dict[str, Any]: ...


@runtime_checkable
class ChannelPort(Protocol):
    """Outreach. Runs its own consent, frequency and quiet-hours checks and may refuse."""

    async def dispatch(
        self,
        *,
        case_id: str,
        customer_id: str,
        merchant_id: str,
        purpose: str,
        language: str,
        subject: str | None,
        body: str,
        now: dt.datetime,
    ) -> dict[str, Any]:
        """Return ``{"status": str, "sent": bool, "cost_minor": int, "reason": str|None}``."""
        ...


@runtime_checkable
class AuditPort(Protocol):
    """Recording what happened. Redaction is the implementation's job, not the graph's."""

    async def record(
        self,
        *,
        event_type: str,
        actor: str,
        actor_kind: str,
        summary: str,
        detail: dict[str, Any],
        case_id: str | None,
        action_id: str | None,
        merchant_id: str | None,
        thread_id: str | None,
        at: dt.datetime,
    ) -> None: ...


@runtime_checkable
class CasePort(Protocol):
    """Persisting the relational read model alongside the graph's own state."""

    async def sync(self, state: dict[str, Any], *, now: dt.datetime) -> None: ...

    async def persist_action(
        self, *, case_id: str, merchant_id: str, action: dict[str, Any], now: dt.datetime
    ) -> None: ...
