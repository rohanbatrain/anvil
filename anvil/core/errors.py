"""The error taxonomy.

Every failure in Anvil is one of these. Each carries a stable ``code`` for the
API surface and a ``retryable`` flag the executor uses to decide between backoff
and escalation -- so retry behaviour is a property of the error type rather than
a judgement call scattered across call sites.
"""

from __future__ import annotations

from typing import Any


class AnvilError(Exception):
    """Base class. Never raised directly."""

    code: str = "anvil_error"
    http_status: int = 500
    retryable: bool = False

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "context": self.context,
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


# --- invariant violations: these mean the system is wrong, not the input ----


class InvariantViolation(AnvilError):
    """A financial invariant from docs/explanation/architecture.md section 6 was broken.

    These are never caught and handled -- they abort the transaction and page a
    human. A system that silently recovers from an unbalanced ledger is a system
    that silently loses money.
    """

    code = "invariant_violation"
    http_status = 500


class UnbalancedTransaction(InvariantViolation):
    code = "unbalanced_transaction"


class LedgerImmutabilityViolation(InvariantViolation):
    code = "ledger_immutability_violation"


# --- domain refusals: the system is right and is saying no ------------------


class DomainError(AnvilError):
    code = "domain_error"
    http_status = 422


class AuthorisationDenied(DomainError):
    code = "authorisation_denied"
    http_status = 403


class StepUpRequired(DomainError):
    code = "step_up_required"
    http_status = 401


class PolicyDenied(DomainError):
    code = "policy_denied"
    http_status = 403


class BudgetExhausted(DomainError):
    code = "budget_exhausted"
    http_status = 409


class ConsentMissing(DomainError):
    code = "consent_missing"
    http_status = 403


class StoppingRuleFired(DomainError):
    code = "stopping_rule_fired"
    http_status = 409


class InsufficientReservation(DomainError):
    code = "insufficient_reservation"
    http_status = 409


# --- conflicts ---------------------------------------------------------------


class ConflictError(AnvilError):
    code = "conflict"
    http_status = 409


class OptimisticLockConflict(ConflictError):
    """Two operators tried to resolve the same approval."""

    code = "optimistic_lock_conflict"


class DuplicateEvent(ConflictError):
    """A webhook we have already processed. Answered with 200, not an error page."""

    code = "duplicate_event"
    http_status = 200


class StaleEvent(ConflictError):
    """An out-of-order webhook older than the state we hold."""

    code = "stale_event"
    http_status = 200


# --- external boundaries -----------------------------------------------------


class ExternalError(AnvilError):
    code = "external_error"
    http_status = 502
    retryable = True


class GatewayError(ExternalError):
    code = "gateway_error"


class GatewayTimeout(ExternalError):
    """The outcome is genuinely unknown. Never blind-retry; reconcile."""

    code = "gateway_timeout"
    http_status = 504
    retryable = False


class WebhookVerificationFailed(AnvilError):
    code = "webhook_verification_failed"
    http_status = 400
    retryable = False


class WebhookReplayRejected(AnvilError):
    code = "webhook_replay_rejected"
    http_status = 400
    retryable = False


class LLMError(ExternalError):
    code = "llm_error"


class LLMTimeout(LLMError):
    code = "llm_timeout"


class LLMRateLimited(LLMError):
    code = "llm_rate_limited"


class StructuredOutputInvalid(LLMError):
    """The model returned something the schema rejects. Retry with the error."""

    code = "structured_output_invalid"
    retryable = True


class ModelProposedOutOfBounds(DomainError):
    """The model proposed an action outside the closed action space or its limits.

    Recorded as a first-class model-safety event and surfaced on the dashboard.
    Hiding these would defeat the point of measuring them.
    """

    code = "model_proposed_out_of_bounds"


class FixtureMissing(AnvilError):
    """Offline mode has no recorded response for this call."""

    code = "fixture_missing"
    http_status = 500


# --- lookup ------------------------------------------------------------------


class NotFound(AnvilError):
    code = "not_found"
    http_status = 404


class ValidationError(AnvilError):
    code = "validation_error"
    http_status = 400
