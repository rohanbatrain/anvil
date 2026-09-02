"""Every ORM model, re-exported so ``Base.metadata`` is always complete.

Importing this package is what registers the full schema with SQLAlchemy.
Alembic, the test fixtures and the application all import from here rather than
from the individual modules, so no code path can ever see a partial schema.
"""

from anvil.db.models.authorisation import (
    Authorisation,
    AuthorisationUsage,
    StepUpChallenge,
)
from anvil.db.models.comms import (
    ConsentReceipt,
    ContactLedger,
    ErasureRequest,
    OutreachMessage,
)
from anvil.db.models.experiment import ArmAssignment, BatchResult, RecoveryBatch
from anvil.db.models.ledger import (
    Account,
    BudgetReservation,
    ConcessionBudget,
    LedgerEntry,
    LedgerTransaction,
)
from anvil.db.models.parties import Customer, Merchant, Plan, Subscription
from anvil.db.models.platform import (
    AuditRecord,
    DomainEvent,
    IdempotencyRecord,
    LLMCall,
    OutboxEntry,
    ProcessedWebhook,
)
from anvil.db.models.policy import (
    Approval,
    PolicyBundle,
    PolicyEvaluation,
    PolicyRule,
)
from anvil.db.models.recovery import PaymentAttempt, RecoveryAction, RecoveryCase

__all__ = [
    "Account",
    "Approval",
    "ArmAssignment",
    "AuditRecord",
    "Authorisation",
    "AuthorisationUsage",
    "BatchResult",
    "BudgetReservation",
    "ConcessionBudget",
    "ConsentReceipt",
    "ContactLedger",
    "Customer",
    "DomainEvent",
    "ErasureRequest",
    "IdempotencyRecord",
    "LLMCall",
    "LedgerEntry",
    "LedgerTransaction",
    "Merchant",
    "OutboxEntry",
    "OutreachMessage",
    "PaymentAttempt",
    "Plan",
    "PolicyBundle",
    "PolicyEvaluation",
    "PolicyRule",
    "ProcessedWebhook",
    "RecoveryAction",
    "RecoveryBatch",
    "RecoveryCase",
    "StepUpChallenge",
    "Subscription",
]
