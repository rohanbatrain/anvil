"""The dependency container the graph's nodes are built over.

Assembled once by :mod:`anvil.graph.wiring` and closed over by the node
functions. Holding it as a frozen dataclass rather than reaching for a global
registry means a test can build a graph with seven stubs and one real
implementation, which is how the degradation paths get exercised honestly.
"""

from __future__ import annotations

from dataclasses import dataclass

from anvil.core.clock import Clock
from anvil.graph.ports import (
    ApprovalPort,
    AuditPort,
    AuthorisationPort,
    CasePort,
    ChannelPort,
    ClassifierPort,
    GatewayPort,
    LedgerPort,
    ModelPort,
    PolicyPort,
    SchedulerPort,
    ScoringPort,
)


@dataclass(frozen=True, slots=True)
class Deps:
    """Everything the recovery graph is allowed to reach."""

    clock: Clock
    classifier: ClassifierPort
    scheduler: SchedulerPort
    scoring: ScoringPort
    model: ModelPort
    authorisation: AuthorisationPort
    policy: PolicyPort
    approvals: ApprovalPort
    ledger: LedgerPort
    gateway: GatewayPort
    channels: ChannelPort
    audit: AuditPort
    cases: CasePort

    #: Actions the planner may propose. Narrower than the full ActionType enum
    #: only if a merchant has disabled something; the executor re-checks anyway.
    allowed_actions: tuple[str, ...] = ()

    @property
    def now(self) -> object:
        return self.clock.now()
