"""Turning a merchant's prose into a deterministic, reviewable policy bundle.

This is the one place a language model touches policy, and the shape of that
contact is the whole point: **the model authors the policy, it never is the
policy.** Compilation happens once, at build time, under human review. After
that the compiled artifact executes with no model anywhere near it, so the
thousandth evaluation is identical to the first.

The pipeline has four stages and a human sits in the middle of it:

1. **Propose.** The model reads prose and emits candidate rules in the same
   expression-tree shape :mod:`anvil.policy.expressions` validates.
2. **Verify.** Every tree is validated structurally, every field name is checked
   against the fact catalogue, and the immutable floor is checked for tampering.
   A model that hallucinates a fact name or quietly drops a consent rule fails
   here, loudly.
3. **Diff.** The proposal is rendered against the active bundle in terms a
   merchant can actually approve -- rules added, removed, and changed, with the
   conditions written back out as English.
4. **Approve.** Only a human activates a bundle. Until then it is ``PROPOSED``
   and the evaluator will not load it.

The model is reached through :class:`PolicyCompilerModel`, a protocol defined
here rather than an import from :mod:`anvil.llm`. That inversion keeps this
module testable with a stub and keeps the dependency pointing the right way: the
policy engine defines what it needs, and the LLM layer satisfies it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from anvil.core.errors import ValidationError
from anvil.core.ids import IdPrefix, new_id
from anvil.domain.enums import PolicyEffect
from anvil.policy.evaluator import CompiledBundle, CompiledRule
from anvil.policy.expressions import MalformedExpression, describe, validate_expression
from anvil.policy.facts import FACT_SPECS
from anvil.policy.hashing import bundle_hash, canonical_rule


class PolicyCompilationRejected(ValidationError):
    """The proposal was refused. Carries every reason, not just the first.

    Returning all failures at once matters: a merchant who fixes one problem,
    resubmits, and discovers a second is being made to play twenty questions
    with their own policy.
    """

    code = "policy_compilation_rejected"

    def __init__(self, message: str, problems: Sequence[str]) -> None:
        super().__init__(message, problems=list(problems))
        self.problems = list(problems)


@dataclass(frozen=True, slots=True)
class ProposedRule:
    """A rule as the model emitted it, before it has been trusted."""

    name: str
    priority: int
    effect: PolicyEffect
    conditions: dict[str, Any]
    description: str
    cap_amount_minor: int | None = None
    cap_percent: int | None = None


@runtime_checkable
class PolicyCompilerModel(Protocol):
    """What the compiler needs from a language model. Deliberately narrow.

    Defined here, satisfied by :mod:`anvil.llm`. The compiler cannot ask the
    model anything except "turn this prose into candidate rules", which bounds
    the blast radius of a compromised or confused model to a proposal that then
    has to survive verification.
    """

    async def compile_policy(
        self, *, prose: str, fact_names: Sequence[str], existing_rule_names: Sequence[str]
    ) -> Sequence[ProposedRule]: ...


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

#: Priorities below this belong to the immutable regulatory floor. A proposal
#: may not place rules there, because doing so would let generated policy run
#: ahead of the consent and authorisation checks.
RESERVED_PRIORITY_CEILING = 100


def verify(proposed: Sequence[ProposedRule], *, active: CompiledBundle | None) -> list[str]:
    """Every reason this proposal must not be activated. Empty means it may be.

    Checks are ordered from structural to semantic so the first problems a
    merchant sees are the concrete ones.
    """
    problems: list[str] = []
    seen_names: set[str] = set()
    # The regulatory floor legitimately occupies the reserved band, and a
    # proposal is expected to carry it through unchanged. Exempting those names
    # from the band check is what lets "carry the floor through" and "stay out
    # of the floor's priorities" both be true at once.
    floor_names = {r.name for r in active.immutable_rules} if active is not None else set()

    for rule in proposed:
        label = rule.name or "<unnamed>"

        if not rule.name.strip():
            problems.append("a proposed rule has no name")
        if rule.name in seen_names:
            problems.append(f"{label!r} is defined twice; rule names must be unique")
        seen_names.add(rule.name)

        # A malformed condition does not suppress the remaining checks. A
        # merchant fixing one problem and immediately meeting a second is
        # exactly the twenty-questions experience this function exists to avoid.
        structurally_valid = True
        try:
            validate_expression(rule.conditions)
        except MalformedExpression as exc:
            problems.append(f"{label!r} has a malformed condition: {exc.message}")
            structurally_valid = False

        if structurally_valid:
            unknown = sorted(_referenced_fields(rule.conditions) - set(FACT_SPECS))
            if unknown:
                problems.append(
                    f"{label!r} tests {', '.join(unknown)}, which "
                    f"{'is not a fact' if len(unknown) == 1 else 'are not facts'} Anvil records"
                )

        if rule.priority < RESERVED_PRIORITY_CEILING and rule.name not in floor_names:
            problems.append(
                f"{label!r} claims priority {rule.priority}, inside the reserved band "
                f"below {RESERVED_PRIORITY_CEILING} where the regulatory floor runs"
            )

        if rule.effect is PolicyEffect.CAP and (
            rule.cap_amount_minor is None and rule.cap_percent is None
        ):
            problems.append(f"{label!r} caps something but names no ceiling")

        if rule.cap_amount_minor is not None and rule.cap_amount_minor <= 0:
            problems.append(f"{label!r} declares a non-positive rupee ceiling")
        if rule.cap_percent is not None and not 0 <= rule.cap_percent <= 100:
            problems.append(f"{label!r} declares a percentage ceiling outside 0-100")

        if not rule.description.strip():
            problems.append(
                f"{label!r} has no description. A merchant approving a rule they cannot "
                "read is not meaningfully approving it"
            )

    if active is not None:
        problems.extend(_immutable_violations(proposed, active))

    return problems


def _referenced_fields(node: Any) -> set[str]:
    """Every fact name a tree reads."""
    if not isinstance(node, dict):
        return set()
    op = node.get("op")
    if op in ("and", "or"):
        found: set[str] = set()
        for child in node.get("args", []):
            found |= _referenced_fields(child)
        return found
    if op == "not":
        return _referenced_fields(node.get("arg"))
    field = node.get("field")
    return {field} if isinstance(field, str) else set()


def _immutable_violations(proposed: Sequence[ProposedRule], active: CompiledBundle) -> list[str]:
    """Immutable rules must survive a recompilation byte for byte.

    Checked by canonical content rather than by name, so a proposal cannot keep
    the name of a consent rule while rewriting what it does.
    """
    problems: list[str] = []
    proposed_by_name = {r.name: r for r in proposed}

    for original in active.immutable_rules:
        candidate = proposed_by_name.get(original.name)
        if candidate is None:
            problems.append(
                f"{original.name!r} is a regulatory rule and cannot be removed. "
                "A merchant cannot consent away a customer's rights on their behalf"
            )
            continue
        rewritten = CompiledRule(
            id=original.id,
            name=candidate.name,
            priority=candidate.priority,
            effect=candidate.effect,
            conditions=candidate.conditions,
            description=candidate.description,
            cap_amount_minor=candidate.cap_amount_minor,
            cap_percent=candidate.cap_percent,
            is_immutable=True,
        )
        if canonical_rule(rewritten) != canonical_rule(original):
            problems.append(
                f"{original.name!r} is a regulatory rule and was modified. "
                "Immutable rules must be carried through unchanged"
            )
    return problems


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleChange:
    name: str
    kind: str  # "added" | "removed" | "changed"
    before: str | None = None
    after: str | None = None
    is_immutable: bool = False


@dataclass(frozen=True, slots=True)
class BundleDiff:
    """What activating this proposal would change, in reviewable terms."""

    changes: tuple[RuleChange, ...]
    from_version: int | None
    to_version: int

    @property
    def is_empty(self) -> bool:
        return not self.changes

    def summary(self) -> str:
        """The text stored on the bundle and shown above the approve button."""
        if self.is_empty:
            return "No behavioural change. The compiled policy is identical to the active one."
        added = [c for c in self.changes if c.kind == "added"]
        removed = [c for c in self.changes if c.kind == "removed"]
        changed = [c for c in self.changes if c.kind == "changed"]
        parts: list[str] = []
        if added:
            parts.append(f"{len(added)} rule(s) added: " + ", ".join(c.name for c in added))
        if changed:
            parts.append(f"{len(changed)} rule(s) changed: " + ", ".join(c.name for c in changed))
        if removed:
            parts.append(f"{len(removed)} rule(s) removed: " + ", ".join(c.name for c in removed))
        return "; ".join(parts)

    def render(self) -> str:
        """A full, line-by-line diff for the policy studio."""
        if self.is_empty:
            return self.summary()
        lines: list[str] = []
        for change in self.changes:
            marker = {"added": "+", "removed": "-", "changed": "~"}[change.kind]
            lock = " [regulatory]" if change.is_immutable else ""
            lines.append(f"{marker} {change.name}{lock}")
            if change.before:
                lines.append(f"    was: {change.before}")
            if change.after:
                lines.append(f"    now: {change.after}")
        return "\n".join(lines)


def _render_rule(rule: CompiledRule | ProposedRule) -> str:
    effect = rule.effect.value.upper()
    ceiling = ""
    if rule.cap_amount_minor is not None:
        ceiling += f" cap {rule.cap_amount_minor / 100:,.2f}"
    if rule.cap_percent is not None:
        ceiling += f" cap {rule.cap_percent}% of MRR"
    return f"[{rule.priority}] {effect}{ceiling} when {describe(rule.conditions)}"


def diff(
    active: CompiledBundle | None, proposed: Sequence[CompiledRule], to_version: int
) -> BundleDiff:
    """Compare a proposal against what is running."""
    before = {r.name: r for r in (active.rules if active else ())}
    after = {r.name: r for r in proposed}

    changes: list[RuleChange] = []
    for name in sorted(set(before) | set(after)):
        old = before.get(name)
        new = after.get(name)
        if old is None and new is not None:
            changes.append(RuleChange(name=name, kind="added", after=_render_rule(new)))
        elif new is None and old is not None:
            changes.append(
                RuleChange(
                    name=name,
                    kind="removed",
                    before=_render_rule(old),
                    is_immutable=old.is_immutable,
                )
            )
        elif old is not None and new is not None and canonical_rule(old) != canonical_rule(new):
            changes.append(
                RuleChange(
                    name=name,
                    kind="changed",
                    before=_render_rule(old),
                    after=_render_rule(new),
                    is_immutable=old.is_immutable,
                )
            )
    return BundleDiff(
        changes=tuple(changes),
        from_version=active.version if active else None,
        to_version=to_version,
    )


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompilationResult:
    """A proposal that passed verification, with its diff and hash."""

    bundle: CompiledBundle
    diff: BundleDiff
    prose: str

    @property
    def content_hash(self) -> str:
        return self.bundle.content_hash

    @property
    def requires_review(self) -> bool:
        """Always true. Stated as a property so no caller can assume otherwise."""
        return True


def assemble(
    proposed: Sequence[ProposedRule],
    *,
    active: CompiledBundle | None,
    prose: str,
    bundle_id: str | None = None,
) -> CompilationResult:
    """Verify a proposal and turn it into a reviewable bundle.

    Raises :class:`PolicyCompilationRejected` with every problem when the
    proposal cannot be trusted. The immutable rules from the active bundle are
    carried through explicitly rather than relied upon to be present, so a model
    that simply forgot them produces a correct bundle and a clear diff.
    """
    problems = verify(proposed, active=active)
    if problems:
        raise PolicyCompilationRejected(
            f"the compiled policy was refused for {len(problems)} reason(s)", problems
        )

    version = (active.version + 1) if active else 1
    carried = list(active.immutable_rules) if active else []
    carried_names = {r.name for r in carried}

    rules: list[CompiledRule] = list(carried)
    for rule in proposed:
        if rule.name in carried_names:
            continue
        rules.append(
            CompiledRule(
                id=new_id(IdPrefix.POLICY_RULE),
                name=rule.name,
                priority=rule.priority,
                effect=rule.effect,
                conditions=rule.conditions,
                description=rule.description,
                cap_amount_minor=rule.cap_amount_minor,
                cap_percent=rule.cap_percent,
                is_immutable=False,
            )
        )

    bundle = CompiledBundle(
        id=bundle_id or new_id(IdPrefix.POLICY_BUNDLE),
        version=version,
        rules=tuple(rules),
        content_hash=bundle_hash(rules),
    )
    bundle.validate()

    return CompilationResult(bundle=bundle, diff=diff(active, bundle.rules, version), prose=prose)


async def compile_policy(
    model: PolicyCompilerModel,
    *,
    prose: str,
    active: CompiledBundle | None,
    bundle_id: str | None = None,
) -> CompilationResult:
    """The full pipeline: ask the model, verify, diff.

    Note what this function does *not* do: activate anything. The result is a
    proposal, and a human turns it on.
    """
    if not prose.strip():
        raise PolicyCompilationRejected("there is nothing to compile", ["the policy text is empty"])
    proposed = await model.compile_policy(
        prose=prose,
        fact_names=sorted(FACT_SPECS),
        existing_rule_names=sorted(r.name for r in (active.rules if active else ())),
    )
    return assemble(proposed, active=active, prose=prose, bundle_id=bundle_id)
