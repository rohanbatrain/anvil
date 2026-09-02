"""The typed state that flows through the recovery graph.

One :class:`RecoveryState` per case, checkpointed to Postgres after every node.
Three properties of this design matter more than its contents.

**It is a TypedDict, not a Pydantic model.** LangGraph merges partial updates
returned by nodes into the state dict, and a node that returns three keys must
not have to reconstruct the other forty. A TypedDict with ``total=False`` says
exactly that: any node may update any subset.

**Every field is JSON-serialisable.** The checkpointer writes this to Postgres
and reads it back after a process restart, so the state holds ids, integers and
plain dicts rather than ORM instances or ``Money`` objects. Amounts are integer
minor units, as everywhere else in Anvil. The cost of that discipline is a
little marshalling at the node boundaries; the benefit is that a case resumed
three days later on a different machine reconstitutes exactly.

**It accumulates rather than overwrites.** ``history`` and ``actions`` grow, and
nothing removes from them. The state at any checkpoint is therefore a complete
account of how the case reached that point, which is what makes the time-travel
replay in :mod:`anvil.audit.replay` show a story rather than a snapshot.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

from anvil.policy.facts import NEVER_CONTACTED_HOURS

# --- What a node can hand back to the router --------------------------------

Route = Literal[
    "classify",
    "enrich",
    "diagnose",
    "score",
    "plan",
    "authorise",
    "step_up",
    "policy",
    "approval",
    "schedule",
    "execute",
    "observe",
    "close",
    "terminate",
]


class ProposedAction(TypedDict, total=False):
    """One step the planner proposed, carried through the graph as plain data.

    It gains fields as it moves: the planner sets ``action_type`` and
    ``rationale``; the authorisation node adds ``authorisation_id`` and
    ``authorisation_decision``; the policy node adds ``policy_effect`` and any
    cap; approval adds the operator's decision. By the time it reaches the
    executor it carries its own complete justification, which is what makes the
    persisted ``RecoveryAction`` row self-explaining.
    """

    action_id: str
    action_type: str
    sequence: int
    amount_minor: NotRequired[int]
    payload: dict[str, Any]
    rationale: str
    model_confidence: NotRequired[int]

    # --- the legitimacy trail, filled in as the action moves through ---------
    authorisation_id: NotRequired[str]
    authorisation_decision: NotRequired[str]
    denial_reason: NotRequired[str]
    policy_bundle_id: NotRequired[str]
    policy_rule_id: NotRequired[str]
    policy_effect: NotRequired[str]
    capped_amount_minor: NotRequired[int]
    approval_id: NotRequired[str]
    reservation_id: NotRequired[str]
    idempotency_key: NotRequired[str]

    # --- scheduling and outcome ----------------------------------------------
    scheduled_for: NotRequired[str]
    expected_probability_bps: NotRequired[int]
    expected_recovery_minor: NotRequired[int]
    status: NotRequired[str]
    outcome: NotRequired[dict[str, Any]]


class HistoryEntry(TypedDict):
    """One line of the case's own narrative, appended by every node.

    Distinct from the audit log: this is the *agent's* working memory, small
    enough to put in a prompt, and it is what the planner reads to avoid
    proposing the thing that just failed.
    """

    at: str
    node: str
    summary: str
    detail: NotRequired[dict[str, Any]]


class RecoveryState(TypedDict, total=False):
    """Everything one recovery case knows about itself."""

    # --- identity -------------------------------------------------------------
    case_id: str
    thread_id: str
    merchant_id: str
    customer_id: str
    subscription_id: str
    batch_id: NotRequired[str]
    correlation_id: str

    # --- the money ------------------------------------------------------------
    amount_at_risk_minor: int
    amount_recovered_minor: int
    concession_granted_minor: int
    currency: str
    subscription_mrr_minor: int

    # --- the failure ----------------------------------------------------------
    raw_failure_code: NotRequired[str]
    raw_failure_description: NotRequired[str]
    bank_narration: NotRequired[str]
    rail_hint: NotRequired[str]
    failure_class: NotRequired[str]
    classified_deterministically: NotRequired[bool]
    classification_confidence_bps: NotRequired[int]
    original_failure_at: str

    # --- what the model concluded ---------------------------------------------
    diagnosis: NotRequired[dict[str, Any]]
    plan_strategy: NotRequired[str]

    # --- scores ---------------------------------------------------------------
    recovery_likelihood: NotRequired[int]
    churn_risk: NotRequired[int]
    priority_score: NotRequired[int]

    # --- context the planner reads --------------------------------------------
    customer_tenure_days: int
    customer_lifetime_value_minor: int
    prior_failures: int
    prior_recoveries: int
    prior_concession_count: int
    prior_concessions_minor: int
    contacts_last_24h: int
    contacts_last_7d: int
    hours_since_last_contact: int
    preferred_language: str

    # --- authorisation --------------------------------------------------------
    authorisation_id: NotRequired[str]
    mandate_attempts_remaining: NotRequired[int]
    mandate_valid_until: NotRequired[str]
    budget_headroom_minor: int
    customer_concession_headroom_minor: int
    consent_state: str
    #: Whether this merchant queues every action for a human. Declared here
    #: rather than passed as loose context because LangGraph filters state
    #: updates to the fields this TypedDict declares -- an undeclared key is
    #: silently dropped, and a dropped review-first flag defaults to True,
    #: which would quietly put every merchant into manual review.
    merchant_review_first: bool

    # --- progress -------------------------------------------------------------
    status: str
    attempts_made: int
    contacts_made: int
    actions: list[ProposedAction]
    current_action_index: NotRequired[int]
    next_action_at: NotRequired[str]

    # --- interrupts -----------------------------------------------------------
    pending_approval_id: NotRequired[str]
    pending_step_up_id: NotRequired[str]
    human_decision: NotRequired[dict[str, Any]]
    step_up_result: NotRequired[dict[str, Any]]

    # --- bookkeeping ----------------------------------------------------------
    history: list[HistoryEntry]
    #: Times the model proposed something the executor refused. Surfaced on the
    #: dashboard as a first-class metric rather than swallowed.
    model_safety_events: int
    #: True once the deterministic fallback has taken over from the model.
    degraded: bool
    degraded_reason: NotRequired[str]
    model_cost_minor: int
    channel_cost_minor: int
    experiment_arm: NotRequired[str]

    # --- terminal -------------------------------------------------------------
    closure_reason: NotRequired[str]
    closed_at: NotRequired[str]


def initial_state(
    *,
    case_id: str,
    thread_id: str,
    merchant_id: str,
    customer_id: str,
    subscription_id: str,
    amount_at_risk_minor: int,
    subscription_mrr_minor: int,
    original_failure_at: str,
    correlation_id: str,
    currency: str = "INR",
    **context: Any,
) -> RecoveryState:
    """Build the state a brand-new case starts from.

    Every counter starts at a real zero rather than being absent, so a node can
    read ``state["attempts_made"]`` without a defensive ``.get`` and a missing
    key means a genuine bug rather than a first run.
    """
    state: RecoveryState = {
        "case_id": case_id,
        "thread_id": thread_id,
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "subscription_id": subscription_id,
        "correlation_id": correlation_id,
        "amount_at_risk_minor": amount_at_risk_minor,
        "amount_recovered_minor": 0,
        "concession_granted_minor": 0,
        "currency": currency,
        "subscription_mrr_minor": subscription_mrr_minor,
        "original_failure_at": original_failure_at,
        "customer_tenure_days": 0,
        "customer_lifetime_value_minor": 0,
        "prior_failures": 0,
        "prior_recoveries": 0,
        "prior_concession_count": 0,
        "prior_concessions_minor": 0,
        "contacts_last_24h": 0,
        "contacts_last_7d": 0,
        # The policy fact catalogue caps this at one year and treats that value
        # as "never contacted"; using its own constant keeps the two in step.
        "hours_since_last_contact": NEVER_CONTACTED_HOURS,
        "preferred_language": "en",
        "budget_headroom_minor": 0,
        "customer_concession_headroom_minor": 0,
        "consent_state": "never_granted",
        "merchant_review_first": True,
        "status": "open",
        "attempts_made": 0,
        "contacts_made": 0,
        "actions": [],
        "history": [],
        "model_safety_events": 0,
        "degraded": False,
        "model_cost_minor": 0,
        "channel_cost_minor": 0,
    }
    for key, value in context.items():
        state[key] = value  # type: ignore[literal-required]
    return state


def note(state: RecoveryState, node: str, summary: str, **detail: Any) -> list[HistoryEntry]:
    """Append one line to the case narrative, returning the whole list.

    Returns the full list rather than the single entry because LangGraph merges
    node returns by replacing keys: a node returning only the new entry would
    erase the history it was appending to.
    """
    entry: HistoryEntry = {
        "at": detail.pop("at", ""),
        "node": node,
        "summary": summary,
    }
    if detail:
        entry["detail"] = detail
    return [*state.get("history", []), entry]


def current_action(state: RecoveryState) -> ProposedAction | None:
    """The action the graph is currently working on, if any."""
    actions = state.get("actions", [])
    index = state.get("current_action_index")
    if index is None or index >= len(actions):
        return None
    return actions[index]


def replace_action(state: RecoveryState, index: int, **updates: Any) -> list[ProposedAction]:
    """Return the action list with one entry updated. Never mutates in place.

    In-place mutation of a checkpointed structure is how a state that looks
    right in memory gets written to Postgres wrong; copying is cheap and the
    lists are short.
    """
    actions = [dict(a) for a in state.get("actions", [])]
    if 0 <= index < len(actions):
        actions[index].update(updates)
    return actions  # type: ignore[return-value]
