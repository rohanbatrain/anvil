"""Canonical hashing of a policy bundle.

"Is this the bundle the merchant approved?" must be answerable by comparison,
not by reading. A bundle is therefore content-addressed: the hash covers every
field that changes what the bundle *does*, and nothing that does not.

Two exclusions are deliberate. Ids and timestamps are excluded, so a bundle
re-imported into a fresh database hashes identically to the one it came from --
otherwise the hash would identify a row rather than a policy. Descriptions are
included, because a rule whose description no longer matches its condition is a
rule a human will approve on false pretences, and that should register as a
change.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any, Protocol


class HashableRule(Protocol):
    """The subset of a rule that determines behaviour."""

    name: str
    priority: int
    effect: Any
    conditions: dict[str, Any]
    cap_amount_minor: int | None
    cap_percent: int | None
    is_immutable: bool
    description: str | None


def canonical_rule(rule: HashableRule) -> dict[str, Any]:
    """One rule reduced to its behavioural content, with keys in a fixed order."""
    return {
        "cap_amount_minor": rule.cap_amount_minor,
        "cap_percent": rule.cap_percent,
        "conditions": rule.conditions,
        "description": rule.description or "",
        "effect": str(getattr(rule.effect, "value", rule.effect)),
        "is_immutable": bool(rule.is_immutable),
        "name": rule.name,
        "priority": rule.priority,
    }


def canonical_bundle(rules: Sequence[HashableRule]) -> str:
    """The exact bytes that get hashed. Kept public so a hash can be explained.

    Rules are sorted by ``(priority, name)`` rather than left in list order,
    because two bundles with the same rules in a different insertion order are
    the same policy and must hash the same.
    """
    ordered = sorted((canonical_rule(r) for r in rules), key=lambda r: (r["priority"], r["name"]))
    return json.dumps(ordered, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def bundle_hash(rules: Sequence[HashableRule]) -> str:
    """A 64-character hex digest identifying this policy's behaviour."""
    return hashlib.blake2b(canonical_bundle(rules).encode("utf-8"), digest_size=32).hexdigest()
