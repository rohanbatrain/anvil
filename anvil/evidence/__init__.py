"""Experiment assignment, honest statistics, and the batch report.

The module that answers "compared to what?". Nothing here rounds an
inconvenient result away: an interval that crosses zero is reported as not
significant, an underpowered batch says so, and a run in which the naive
baseline wins says that too.
"""

from anvil.evidence.assignment import (
    DEFAULT_SPLIT,
    EVEN_SPLIT,
    ArmSplit,
    Assignment,
    assign,
    assign_all,
    realised_split,
)
from anvil.evidence.metrics import (
    ArmResult,
    BatchSummary,
    Comparison,
    EmptyControlArm,
    aggregate,
    as_json,
    compare,
    conserves_money,
    summarise,
)
from anvil.evidence.report import render
from anvil.evidence.statistics import (
    Interval,
    bootstrap_difference,
    bootstrap_proportion,
    is_significant,
    minimum_detectable_effect_bps,
    two_proportion_z,
)

__all__ = [
    "DEFAULT_SPLIT",
    "EVEN_SPLIT",
    "ArmResult",
    "ArmSplit",
    "Assignment",
    "BatchSummary",
    "Comparison",
    "EmptyControlArm",
    "Interval",
    "aggregate",
    "as_json",
    "assign",
    "assign_all",
    "bootstrap_difference",
    "bootstrap_proportion",
    "compare",
    "conserves_money",
    "is_significant",
    "minimum_detectable_effect_bps",
    "realised_split",
    "render",
    "summarise",
    "two_proportion_z",
]
