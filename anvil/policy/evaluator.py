"""Evaluating a policy bundle against a fact set.

Pure, total and side-effect free. The same facts and the same bundle always
produce the same decision, and the decision records which rule produced it --
which is what makes ``PolicyEvaluation`` worth storing and what makes "why was
this allowed?" answerable months later.

The four semantics below are choices, and each exists to close a specific way a
policy engine can quietly permit something:

**The first matching DENY wins outright and stops evaluation.** A later ALLOW
cannot rescue a denied action. Without this, rule ordering becomes a subtle
source of permission, and merchants would have to reason about the whole bundle
to know whether a prohibition holds.

**REQUIRE_APPROVAL is sticky.** Once any rule has escalated an action to a
human, no subsequent rule can drop it back to an unattended ALLOW. Escalation
is a floor, not a vote.

**The tightest CAP wins, and caps accumulate.** Every matching cap is applied,
and the smallest survives. A bundle with two overlapping ceilings enforces the
lower one, which is the only reading that cannot be gamed by adding rules.

**No match denies.** An action nobody wrote a rule about is refused, not
allowed. This is the single most important line in the module: it means a gap in
the policy is a blocked action rather than an unbounded one, so forgetting to
write a rule is safe.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from anvil.core.errors import PolicyDenied
from anvil.domain.enums import PolicyEffect
from anvil.domain.money import Currency, Money
from anvil.policy.expressions import (
    MalformedExpression,
    describe,
    evaluate_expression,
    validate_expression,
)
from anvil.policy.facts import PolicyFacts


@dataclass(frozen=True, slots=True)
class CompiledRule:
    """A rule detached from the database, so evaluation needs no session."""

    id: str
    name: str
    priority: int
    effect: PolicyEffect
    conditions: dict[str, Any]
    description: str | None = None
    cap_amount_minor: int | None = None
    cap_percent: int | None = None
    is_immutable: bool = False

    def validate(self) -> None:
        validate_expression(self.conditions)
        if self.effect is PolicyEffect.CAP and (
            self.cap_amount_minor is None and self.cap_percent is None
        ):
            raise MalformedExpression(
                f"rule {self.name!r} has effect CAP but declares no ceiling",
                rule=self.name,
            )

    def ceiling_for(self, facts: PolicyFacts) -> Money | None:
        """The absolute ceiling this rule imposes on the proposed amount.

        A percentage cap is resolved against the subscription's monthly value
        rather than against the proposed amount, because "no more than 15%" in a
        merchant's head means 15% of what the customer pays, not 15% of whatever
        the agent happened to propose.
        """
        candidates: list[Money] = []
        if self.cap_amount_minor is not None:
            candidates.append(Money(self.cap_amount_minor, facts.currency))
        if self.cap_percent is not None and facts.subscription_mrr_minor > 0:
            candidates.append(
                Money(facts.subscription_mrr_minor, facts.currency).percent(self.cap_percent)
            )
        if not candidates:
            return None
        tightest = candidates[0]
        for c in candidates[1:]:
            tightest = tightest.min(c)
        return tightest


@dataclass(frozen=True, slots=True)
class CompiledBundle:
    """An ordered, validated set of rules plus the identity of the bundle."""

    id: str
    version: int
    rules: tuple[CompiledRule, ...]
    content_hash: str = ""

    def validate(self) -> None:
        for rule in self.rules:
            rule.validate()

    @property
    def ordered(self) -> tuple[CompiledRule, ...]:
        return tuple(sorted(self.rules, key=lambda r: (r.priority, r.name)))

    @property
    def immutable_rules(self) -> tuple[CompiledRule, ...]:
        return tuple(r for r in self.rules if r.is_immutable)


@dataclass(frozen=True, slots=True)
class RuleTrace:
    """What one rule did. Recorded whether or not it matched.

    Non-matching rules are kept because the useful debugging question is almost
    always "why did the rule I expected to fire not fire?", and a trace that
    only lists matches cannot answer it.
    """

    rule_id: str
    rule_name: str
    priority: int
    effect: PolicyEffect
    matched: bool
    condition_summary: str
    cap_applied_minor: int | None = None
    stopped_evaluation: bool = False

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "priority": self.priority,
            "effect": self.effect.value,
            "matched": self.matched,
            "condition": self.condition_summary,
            "cap_applied_minor": self.cap_applied_minor,
            "stopped_evaluation": self.stopped_evaluation,
        }


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The outcome, with everything needed to justify and replay it."""

    effect: PolicyEffect
    bundle_id: str
    bundle_version: int
    facts: PolicyFacts
    trace: tuple[RuleTrace, ...] = ()
    matched_rule_id: str | None = None
    matched_rule_name: str | None = None
    capped_amount_minor: int | None = None
    reason: str = ""
    capping_rule_name: str | None = None
    _approval_reasons: tuple[str, ...] = field(default=())

    @property
    def allowed(self) -> bool:
        """True only for an unattended ALLOW. Approval-required is not allowed yet."""
        return self.effect is PolicyEffect.ALLOW

    @property
    def denied(self) -> bool:
        return self.effect is PolicyEffect.DENY

    @property
    def requires_approval(self) -> bool:
        return self.effect is PolicyEffect.REQUIRE_APPROVAL

    @property
    def approval_reasons(self) -> tuple[str, ...]:
        return self._approval_reasons

    @property
    def effective_amount(self) -> Money:
        """The amount the executor may actually use, after any cap."""
        minor = (
            self.capped_amount_minor
            if self.capped_amount_minor is not None
            else self.facts.amount_minor
        )
        return Money(minor, self.facts.currency)

    @property
    def was_capped(self) -> bool:
        return (
            self.capped_amount_minor is not None
            and self.capped_amount_minor < self.facts.amount_minor
        )

    def raise_if_denied(self) -> None:
        if self.denied:
            raise PolicyDenied(
                self.reason,
                bundle_id=self.bundle_id,
                bundle_version=self.bundle_version,
                rule=self.matched_rule_name,
                action_type=self.facts.action_type.value,
            )

    def trace_json(self) -> list[dict[str, Any]]:
        return [t.to_json_dict() for t in self.trace]


#: Returned when a bundle contains no rules at all. Named so the reason for the
#: denial is unmistakable in a log: an empty policy is not a permissive policy.
EMPTY_BUNDLE_REASON = (
    "the active policy bundle contains no rules, so nothing is permitted. "
    "An empty policy denies everything by design; it is not a licence to act."
)

NO_MATCH_REASON = (
    "no policy rule matched this action, and Anvil denies what no rule permits. "
    "Add a rule covering it if it should be allowed."
)


def evaluate(bundle: CompiledBundle, facts: PolicyFacts) -> PolicyDecision:
    """Run the bundle against the facts. Pure, total, and never raises on data.

    The only exception this can raise is :class:`MalformedExpression`, and that
    signals a corrupt bundle rather than a business outcome -- a rule that
    cannot be evaluated must stop the world, because the alternative is treating
    "I could not check this constraint" as "this constraint passed".
    """
    trace: list[RuleTrace] = []
    effect = PolicyEffect.ALLOW
    matched_rule: CompiledRule | None = None
    approval_reasons: list[str] = []
    cap: Money | None = None
    capping_rule: CompiledRule | None = None
    any_match = False

    if not bundle.rules:
        return PolicyDecision(
            effect=PolicyEffect.DENY,
            bundle_id=bundle.id,
            bundle_version=bundle.version,
            facts=facts,
            reason=EMPTY_BUNDLE_REASON,
        )

    fact_map = facts.to_json_dict()

    for rule in bundle.ordered:
        matched = evaluate_expression(rule.conditions, fact_map)
        summary = describe(rule.conditions)

        if not matched:
            trace.append(
                RuleTrace(
                    rule_id=rule.id,
                    rule_name=rule.name,
                    priority=rule.priority,
                    effect=rule.effect,
                    matched=False,
                    condition_summary=summary,
                )
            )
            continue

        any_match = True

        if rule.effect is PolicyEffect.DENY:
            trace.append(
                RuleTrace(
                    rule_id=rule.id,
                    rule_name=rule.name,
                    priority=rule.priority,
                    effect=rule.effect,
                    matched=True,
                    condition_summary=summary,
                    stopped_evaluation=True,
                )
            )
            return PolicyDecision(
                effect=PolicyEffect.DENY,
                bundle_id=bundle.id,
                bundle_version=bundle.version,
                facts=facts,
                trace=tuple(trace),
                matched_rule_id=rule.id,
                matched_rule_name=rule.name,
                reason=rule.description or f"denied by policy rule {rule.name!r}",
            )

        cap_applied: int | None = None
        if rule.effect is PolicyEffect.CAP:
            ceiling = rule.ceiling_for(facts)
            if ceiling is not None:
                cap_applied = ceiling.minor
                if cap is None or ceiling < cap:
                    cap = ceiling
                    capping_rule = rule

        if rule.effect is PolicyEffect.REQUIRE_APPROVAL:
            effect = PolicyEffect.REQUIRE_APPROVAL
            matched_rule = rule
            approval_reasons.append(
                rule.description or f"rule {rule.name!r} requires a human decision"
            )

        if rule.effect is PolicyEffect.ALLOW and matched_rule is None:
            matched_rule = rule

        trace.append(
            RuleTrace(
                rule_id=rule.id,
                rule_name=rule.name,
                priority=rule.priority,
                effect=rule.effect,
                matched=True,
                condition_summary=summary,
                cap_applied_minor=cap_applied,
            )
        )

    if not any_match:
        return PolicyDecision(
            effect=PolicyEffect.DENY,
            bundle_id=bundle.id,
            bundle_version=bundle.version,
            facts=facts,
            trace=tuple(trace),
            reason=NO_MATCH_REASON,
        )

    capped_minor: int | None = None
    if cap is not None:
        capped_minor = min(cap.minor, facts.amount_minor)

    if effect is PolicyEffect.REQUIRE_APPROVAL:
        reason = "; ".join(approval_reasons)
    else:
        reason = (
            matched_rule.description or f"permitted by rule {matched_rule.name!r}"
            if matched_rule
            else "permitted"
        )
    if cap is not None and capped_minor is not None and capped_minor < facts.amount_minor:
        reason = (
            f"{reason}. Capped from {Money(facts.amount_minor, facts.currency)} to "
            f"{Money(capped_minor, facts.currency)} by "
            f"{capping_rule.name if capping_rule else 'a cap rule'}"
        )

    return PolicyDecision(
        effect=effect,
        bundle_id=bundle.id,
        bundle_version=bundle.version,
        facts=facts,
        trace=tuple(trace),
        matched_rule_id=matched_rule.id if matched_rule else None,
        matched_rule_name=matched_rule.name if matched_rule else None,
        capped_amount_minor=capped_minor,
        reason=reason,
        capping_rule_name=capping_rule.name if capping_rule else None,
        _approval_reasons=tuple(approval_reasons),
    )


def from_orm_rules(
    bundle_id: str, version: int, rows: Sequence[Any], content_hash: str = ""
) -> CompiledBundle:
    """Adapt persisted ``PolicyRule`` rows into an evaluable bundle."""
    return CompiledBundle(
        id=bundle_id,
        version=version,
        content_hash=content_hash,
        rules=tuple(
            CompiledRule(
                id=row.id,
                name=row.name,
                priority=row.priority,
                effect=row.effect,
                conditions=row.conditions,
                description=row.description,
                cap_amount_minor=row.cap_amount_minor,
                cap_percent=row.cap_percent,
                is_immutable=row.is_immutable,
            )
            for row in rows
        ),
    )


__all__ = [
    "EMPTY_BUNDLE_REASON",
    "NO_MATCH_REASON",
    "CompiledBundle",
    "CompiledRule",
    "Currency",
    "PolicyDecision",
    "RuleTrace",
    "evaluate",
    "from_orm_rules",
]
