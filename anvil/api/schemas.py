"""Response models for the console API.

Every money field crosses the wire twice: once as an integer count of minor
units, which is what any client doing arithmetic must use, and once as a
pre-formatted display string. That looks redundant and is not. Indian digit
grouping is a presentation rule the backend already implements correctly in
:meth:`anvil.domain.money.Money.format`, and having each client reimplement it
is how a dashboard ends up showing 1,234,567 next to 12,34,567 on the same
screen.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from anvil.domain.money import Money


class Amount(BaseModel):
    """A monetary value, exact and displayable."""

    model_config = ConfigDict(frozen=True)

    minor: int = Field(description="Integer count of minor units, e.g. paise.")
    currency: str
    display: str = Field(description="Formatted with Indian digit grouping.")

    @classmethod
    def of(cls, money: Money) -> Amount:
        return cls(minor=money.minor, currency=money.currency.value, display=money.format())


class Health(BaseModel):
    status: str
    mode: str
    version: str
    database: str
    model: str
    seed: int


# --- the scheduler explorer -------------------------------------------------


class RankedHour(BaseModel):
    at: str
    ist_label: str
    probability_bps: int
    value: Amount
    is_chosen: bool


class ScheduleExplanation(BaseModel):
    """The whole decision, including the candidates that lost.

    The ranked list is the point of this endpoint: a chosen hour on its own is
    an assertion, while a chosen hour next to the twenty-three it beat is an
    argument the reader can check.
    """

    should_retry: bool
    failure_class: str
    posture: str
    attempt_number: int
    attempts_remaining: int
    at: str | None
    ist_label: str | None
    probability_bps: int
    expected_value: Amount
    explanation: str
    refusal_reason: str | None
    rationale: str = Field(description="Why this failure class behaves the way it does.")
    ranked: list[RankedHour]


# --- policy -----------------------------------------------------------------


class PolicyRuleView(BaseModel):
    id: str
    name: str
    priority: int
    effect: str
    description: str | None
    condition: str = Field(description="The expression tree rendered as English.")
    cap_amount: Amount | None
    cap_percent: int | None
    is_immutable: bool


class PolicyBundleView(BaseModel):
    id: str
    version: int
    content_hash: str
    rule_count: int
    immutable_count: int
    rules: list[PolicyRuleView]


class RuleTraceView(BaseModel):
    rule_name: str
    priority: int
    effect: str
    matched: bool
    condition: str
    stopped_evaluation: bool


class PolicyDecisionView(BaseModel):
    effect: str
    allowed: bool
    requires_approval: bool
    denied: bool
    matched_rule_name: str | None
    reason: str
    proposed: Amount
    effective: Amount
    was_capped: bool
    capping_rule_name: str | None
    trace: list[RuleTraceView]


# --- the ledger -------------------------------------------------------------


class LedgerEntryView(BaseModel):
    account: str
    direction: str
    amount: Amount


class LedgerTransactionView(BaseModel):
    txn_type: str
    narration: str
    idempotency_key: str
    balances: bool
    total_debits: Amount
    total_credits: Amount
    entries: list[LedgerEntryView]


# --- cases ------------------------------------------------------------------


class TimelineItem(BaseModel):
    node: str
    summary: str
    at: str


class ActionView(BaseModel):
    action_id: str
    action_type: str
    status: str
    amount: Amount | None
    rationale: str | None
    authorisation_decision: str | None
    policy_effect: str | None
    scheduled_for: str | None


class CaseSummary(BaseModel):
    case_id: str
    customer_name: str
    customer_id: str
    arm: str
    status: str
    at_risk: Amount
    recovered: Amount
    failure_class: str | None
    observed_failure_class: str | None
    raw_code: str
    code_is_unmapped: bool
    classified_deterministically: bool | None
    attempts: int
    contacts: int
    bank: str
    mandate_type: str
    recovered_flag: bool


class CaseDetail(CaseSummary):
    narration: str
    failed_at: str
    mandate_reference: str
    mandate_max: Amount
    tenure_days: int
    language: str
    timeline: list[TimelineItem]
    actions: list[ActionView]
    ledger: list[LedgerTransactionView]
    degraded: bool
    degraded_reason: str | None
    model_safety_events: int


# --- approvals --------------------------------------------------------------


class ApprovalItem(BaseModel):
    approval_id: str
    case_id: str
    customer_name: str
    action_type: str
    amount: Amount | None
    rationale: str | None
    escalation_reason: str
    requested_at: str
    at_risk: Amount
    failure_class: str | None
    recovery_likelihood: int | None
    churn_risk: int | None
    version: int


class ApprovalDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject|edit)$")
    decided_by: str
    note: str | None = None
    edited_amount_minor: int | None = Field(default=None, gt=0)
    #: The version the operator was shown. A mismatch means somebody else acted
    #: first, and the second reviewer must see the new state rather than
    #: silently overwrite it.
    version: int


class ApprovalResult(BaseModel):
    approval_id: str
    outcome: str
    case_status: str
    recovered: Amount
    timeline: list[TimelineItem]


# --- the batch --------------------------------------------------------------


class ArmView(BaseModel):
    arm: str
    label: str
    cases: int
    recovered_count: int
    rate_bps: int
    rate_ci_low_bps: int
    rate_ci_high_bps: int
    at_risk: Amount
    recovered: Amount
    net_recovered: Amount
    total_cost: Amount
    attempts: int
    contacts: int
    by_failure_class: dict[str, list[int]]


class ComparisonView(BaseModel):
    treatment: str
    against: str
    difference_bps: int
    ci_low_bps: int
    ci_high_bps: int
    significant: bool
    underpowered: bool
    minimum_detectable_bps: int
    z_score: float
    net_difference: Amount
    verdict: str = Field(description="Stated in words, including when it is not significant.")


class CalibrationBucketView(BaseModel):
    label: str
    count: int
    predicted_bps: int
    observed_bps: int
    gap_bps: int


class BatchView(BaseModel):
    seed: int
    population_size: int
    case_count: int
    total_at_risk: Amount
    model_available: bool
    arms: list[ArmView]
    comparisons: list[ComparisonView]
    unmapped_codes: int
    classified_deterministically: int
    classified_by_model: int
    model_safety_events: int
    calibration_verdict: str
    calibration_buckets: list[CalibrationBucketView]
    limitations: list[str]


# --- taxonomy ---------------------------------------------------------------


class FailureClassView(BaseModel):
    failure_class: str
    posture: str
    retryable: bool
    max_attempts: int
    rationale: str
    example_codes: list[str]


class TaxonomyView(BaseModel):
    total_codes: int
    by_namespace: dict[str, int]
    classes: list[FailureClassView]


class ClassifyRequest(BaseModel):
    raw_code: str | None = None
    gateway_description: str | None = None
    bank_narration: str | None = None
    rail_hint: str | None = None


class ClassifyResult(BaseModel):
    resolved: bool
    failure_class: str | None
    confidence_bps: int | None
    matched_code: str | None
    matched_namespace: str | None
    escalation_reason: str | None
    would_call_model: bool
    detail: dict[str, Any] = Field(default_factory=dict)
