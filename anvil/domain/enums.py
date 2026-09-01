"""The shared vocabulary of the system.

Every cross-module value is a member of an enum defined here. Nothing in Anvil
passes a bare string where one of these types will do, and the LLM layer
constrains model output to exactly these members -- a model can never invent a
failure class or an action type that the executor does not know how to refuse.
"""

from __future__ import annotations

from enum import StrEnum


# --------------------------------------------------------------------- runtime


class RunMode(StrEnum):
    """Offline is the default and needs no external credentials."""

    OFFLINE = "offline"
    LIVE = "live"


# ------------------------------------------------------------------- failures


class FailureClass(StrEnum):
    """The closed set of recovery postures a debit failure can map to.

    See ``docs/ARCHITECTURE.md`` section 7. This is the *only* vocabulary the
    classifier may emit; unrecognised issuer codes fall to ``UNKNOWN`` rather
    than expanding the set.
    """

    INSUFFICIENT_FUNDS = "insufficient_funds"
    INSTRUMENT_EXPIRED = "instrument_expired"
    ISSUER_TECHNICAL = "issuer_technical"
    LIMIT_EXCEEDED = "limit_exceeded"
    MANDATE_REVOKED = "mandate_revoked"
    MANDATE_PAUSED = "mandate_paused"
    ACCOUNT_CLOSED = "account_closed"
    RISK_DECLINED = "risk_declined"
    AUTH_REQUIRED = "auth_required"
    UNKNOWN = "unknown"


class RetryPosture(StrEnum):
    """What retrying this class of failure is worth."""

    RETRY_FAST = "retry_fast"          # transient; retry within hours
    RETRY_SCHEDULED = "retry_scheduled"  # retry, but timing matters a great deal
    RETRY_ONCE = "retry_once"          # one conservative attempt, then stop
    DEFERRED = "deferred"              # blocked on an external state change
    NEVER = "never"                    # retrying is useless or actively harmful


# ------------------------------------------------------------- authorisations


class AuthorisationType(StrEnum):
    UPI_AUTOPAY = "upi_autopay"
    ENACH = "enach"
    CARD_MANDATE = "card_mandate"
    RESERVE_PAY = "reserve_pay"        # Single Block Multi Debit
    DELEGATED_AGENT = "delegated_agent"  # modelled on UPI Circle


class AuthorisationStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    REVOKED = "revoked"
    EXPIRED = "expired"
    EXHAUSTED = "exhausted"            # a Reserve Pay block fully consumed


class AuthorisationDecision(StrEnum):
    AUTHORISED = "authorised"
    REQUIRES_STEP_UP = "requires_step_up"
    DENIED = "denied"


class DenialReason(StrEnum):
    """Why an authorisation check failed closed. Always recorded."""

    NO_AUTHORISATION = "no_authorisation"
    AMOUNT_EXCEEDS_MANDATE = "amount_exceeds_mandate"
    AMOUNT_EXCEEDS_DELEGATION = "amount_exceeds_delegation"
    PERIOD_CAP_EXCEEDED = "period_cap_exceeded"
    OUTSIDE_VALIDITY_WINDOW = "outside_validity_window"
    FREQUENCY_VIOLATION = "frequency_violation"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    BLOCK_INSUFFICIENT = "block_insufficient"
    STATUS_NOT_ACTIVE = "status_not_active"
    COUNTERPARTY_MISMATCH = "counterparty_mismatch"


# ------------------------------------------------------------------- ledger


class EntryDirection(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


class AccountKind(StrEnum):
    """Chart-of-accounts categories. Determines normal balance and sign handling."""

    ASSET = "asset"
    LIABILITY = "liability"
    REVENUE = "revenue"
    EXPENSE = "expense"
    CONTRA_REVENUE = "contra_revenue"   # concessions granted


class LedgerTxnType(StrEnum):
    MANDATE_DEBIT_SETTLED = "mandate_debit_settled"
    CONCESSION_GRANTED = "concession_granted"
    CONCESSION_RESERVED = "concession_reserved"
    CONCESSION_RELEASED = "concession_released"
    BUDGET_FUNDED = "budget_funded"
    CHANNEL_COST = "channel_cost"
    MODEL_COST = "model_cost"
    WRITE_OFF = "write_off"
    REVERSAL = "reversal"


# --------------------------------------------------------------- recovery case


class CaseStatus(StrEnum):
    """Lifecycle of one recovery case. Terminal states are marked."""

    OPEN = "open"
    DIAGNOSING = "diagnosing"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_STEP_UP = "awaiting_step_up"
    SCHEDULED = "scheduled"
    EXECUTING = "executing"
    PENDING_RECONCILIATION = "pending_reconciliation"
    RECOVERED = "recovered"            # terminal, success
    ABANDONED = "abandoned"            # terminal, stopping rule fired
    UNRECOVERABLE = "unrecoverable"    # terminal, terminal failure class
    CHURNED = "churned"                # terminal, customer left

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_CASE_STATUSES

    @property
    def is_waiting_on_human(self) -> bool:
        return self in (CaseStatus.AWAITING_APPROVAL, CaseStatus.AWAITING_STEP_UP)


_TERMINAL_CASE_STATUSES = frozenset(
    {
        CaseStatus.RECOVERED,
        CaseStatus.ABANDONED,
        CaseStatus.UNRECOVERABLE,
        CaseStatus.CHURNED,
    }
)


# ------------------------------------------------------------------- actions


class ActionType(StrEnum):
    """The closed action space. The planner may propose nothing outside this set.

    Grouped by whether the action moves money, because the authorisation and
    approval rules differ sharply between the two groups.
    """

    # --- recovery actions: no concession, but may move money ---------------
    RETRY_DEBIT = "retry_debit"
    SPLIT_DEBIT = "split_debit"
    REQUEST_INSTRUMENT_UPDATE = "request_instrument_update"
    SEND_PAYMENT_LINK = "send_payment_link"
    REQUEST_MANDATE_REAUTH = "request_mandate_reauth"
    TRIGGER_STEP_UP = "trigger_step_up"

    # --- outreach: no money movement ---------------------------------------
    SEND_REMINDER = "send_reminder"
    SEND_DUNNING_NOTICE = "send_dunning_notice"

    # --- commercial levers: draw against the concession budget --------------
    GRANT_GRACE_PERIOD = "grant_grace_period"
    OFFER_PARTIAL_PAYMENT = "offer_partial_payment"
    OFFER_PLAN_DOWNGRADE = "offer_plan_downgrade"
    OFFER_WINBACK_DISCOUNT = "offer_winback_discount"

    # --- terminal ------------------------------------------------------------
    ESCALATE_TO_HUMAN = "escalate_to_human"
    STOP_AND_WRITE_OFF = "stop_and_write_off"
    MARK_CHURNED = "mark_churned"

    @property
    def moves_money(self) -> bool:
        return self in _MONEY_MOVING_ACTIONS

    @property
    def is_concession(self) -> bool:
        return self in _CONCESSION_ACTIONS

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_ACTIONS


_MONEY_MOVING_ACTIONS = frozenset(
    {ActionType.RETRY_DEBIT, ActionType.SPLIT_DEBIT, ActionType.OFFER_PARTIAL_PAYMENT}
)
_CONCESSION_ACTIONS = frozenset(
    {
        ActionType.GRANT_GRACE_PERIOD,
        ActionType.OFFER_PARTIAL_PAYMENT,
        ActionType.OFFER_PLAN_DOWNGRADE,
        ActionType.OFFER_WINBACK_DISCOUNT,
    }
)
_TERMINAL_ACTIONS = frozenset(
    {ActionType.STOP_AND_WRITE_OFF, ActionType.MARK_CHURNED, ActionType.ESCALATE_TO_HUMAN}
)


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    DENIED_BY_POLICY = "denied_by_policy"
    DENIED_BY_AUTHORISATION = "denied_by_authorisation"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    SCHEDULED = "scheduled"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN_OUTCOME = "unknown_outcome"   # gateway timeout; needs reconciliation
    EXPIRED = "expired"
    CANCELLED = "cancelled"


# -------------------------------------------------------------------- policy


class PolicyEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"
    CAP = "cap"


class PolicyBundleStatus(StrEnum):
    DRAFT = "draft"
    PROPOSED = "proposed"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


# ------------------------------------------------------------------- channels


class Channel(StrEnum):
    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    PUSH = "push"
    IN_APP = "in_app"
    VOICE = "voice"


class MessagePurpose(StrEnum):
    """DPDPA purposes. Consent is granted per purpose, never in general."""

    PAYMENT_FAILURE_NOTICE = "payment_failure_notice"
    PAYMENT_RECOVERY_OUTREACH = "payment_recovery_outreach"
    INSTRUMENT_UPDATE_REQUEST = "instrument_update_request"
    MANDATE_REAUTHORISATION = "mandate_reauthorisation"
    STEP_UP_AUTHENTICATION = "step_up_authentication"
    PROMOTIONAL_WINBACK = "promotional_winback"

    @property
    def is_transactional(self) -> bool:
        """Transactional purposes are service messages; promotional ones are not."""
        return self is not MessagePurpose.PROMOTIONAL_WINBACK


class DeliveryStatus(StrEnum):
    QUEUED = "queued"
    SUPPRESSED_NO_CONSENT = "suppressed_no_consent"
    SUPPRESSED_FREQUENCY_CAP = "suppressed_frequency_cap"
    SUPPRESSED_QUIET_HOURS = "suppressed_quiet_hours"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    RESPONDED = "responded"
    BOUNCED = "bounced"
    FAILED = "failed"


# -------------------------------------------------------------------- consent


class ConsentState(StrEnum):
    GRANTED = "granted"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"
    NEVER_GRANTED = "never_granted"


class ErasureStatus(StrEnum):
    REQUESTED = "requested"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    PARTIALLY_FAILED = "partially_failed"
    DEAD_LETTERED = "dead_lettered"


# ----------------------------------------------------------------- experiment


class ExperimentArm(StrEnum):
    """Assignment is a deterministic hash, so a rerun reproduces it exactly."""

    CONTROL = "control"        # no intervention at all
    BASELINE = "baseline"      # fixed-schedule dunning
    ANVIL = "anvil"            # the full agent


# ---------------------------------------------------------------- human loop


class InterruptKind(StrEnum):
    HUMAN_APPROVAL = "human_approval"     # merchant operator must decide
    AFA_STEP_UP = "afa_step_up"           # customer must re-authenticate


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"        # operator amends the payload, then approves


# --------------------------------------------------------------------- audit


class AuditEventType(StrEnum):
    CASE_OPENED = "case_opened"
    FAILURE_CLASSIFIED = "failure_classified"
    DIAGNOSIS_PRODUCED = "diagnosis_produced"
    PLAN_PRODUCED = "plan_produced"
    AUTHORISATION_CHECKED = "authorisation_checked"
    POLICY_EVALUATED = "policy_evaluated"
    MODEL_SAFETY_EVENT = "model_safety_event"   # model proposed out-of-bounds
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    STEP_UP_REQUESTED = "step_up_requested"
    STEP_UP_RESOLVED = "step_up_resolved"
    ACTION_EXECUTED = "action_executed"
    ACTION_OUTCOME = "action_outcome"
    LEDGER_POSTED = "ledger_posted"
    MESSAGE_DISPATCHED = "message_dispatched"
    CONSENT_CHANGED = "consent_changed"
    ERASURE_PROCESSED = "erasure_processed"
    CASE_CLOSED = "case_closed"
    WEBHOOK_RECEIVED = "webhook_received"
    WEBHOOK_REJECTED = "webhook_rejected"


class LLMCallKind(StrEnum):
    CLASSIFY = "classify"
    DIAGNOSE = "diagnose"
    PLAN = "plan"
    COMPOSE = "compose"
    COMPILE_POLICY = "compile_policy"
