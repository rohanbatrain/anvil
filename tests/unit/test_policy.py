"""Policy engine tests.

The evaluator's semantics get exhaustive coverage because each of them closes a
specific way a policy engine can quietly permit something, and a regression in
any one of them would be invisible in ordinary use and catastrophic in an audit.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from anvil.domain.enums import (
    ActionType,
    AuthorisationDecision,
    ConsentState,
    FailureClass,
    MessagePurpose,
    PolicyEffect,
)
from anvil.domain.money import Money
from anvil.policy.compiler import (
    PolicyCompilationRejected,
    ProposedRule,
    assemble,
    compile_policy,
    diff,
    verify,
)
from anvil.policy.defaults import (
    APPROVAL_THRESHOLD_MINOR,
    MAX_CONCESSION_MINOR,
    MAX_CONCESSION_PERCENT_OF_MRR,
    MAX_CONTACTS_24H,
    QUIET_HOURS_START,
    default_bundle,
    immutable_rule_names,
)
from anvil.policy.evaluator import (
    CompiledBundle,
    CompiledRule,
    evaluate,
)
from anvil.policy.expressions import MalformedExpression
from anvil.policy.facts import PolicyFacts
from anvil.policy.hashing import bundle_hash

ALWAYS: dict[str, Any] = {"op": "always"}
NEVER: dict[str, Any] = {"op": "never"}


def rule(
    name: str,
    priority: int,
    effect: PolicyEffect,
    conditions: dict[str, Any] | None = None,
    **kwargs: Any,
) -> CompiledRule:
    return CompiledRule(
        id=f"prl_{name}",
        name=name,
        priority=priority,
        effect=effect,
        conditions=conditions if conditions is not None else ALWAYS,
        description=f"test rule {name}",
        **kwargs,
    )


def bundle(*rules: CompiledRule) -> CompiledBundle:
    return CompiledBundle(id="pol_test", version=1, rules=rules)


def facts(**kwargs: Any) -> PolicyFacts:
    base: dict[str, Any] = {"action_type": ActionType.SEND_REMINDER}
    base.update(kwargs)
    return PolicyFacts(**base)


# ---------------------------------------------------------------------------
# The four semantics
# ---------------------------------------------------------------------------


def test_no_match_denies() -> None:
    """The most important line in the module: a gap in policy blocks."""
    decision = evaluate(bundle(rule("never-fires", 100, PolicyEffect.ALLOW, NEVER)), facts())
    assert decision.denied
    assert "denies what no rule permits" in decision.reason


def test_an_empty_bundle_denies_everything() -> None:
    decision = evaluate(bundle(), facts())
    assert decision.denied
    assert "not a licence to act" in decision.reason


def test_first_deny_wins_and_stops_evaluation() -> None:
    """A later ALLOW cannot rescue a denied action."""
    decision = evaluate(
        bundle(
            rule("deny-early", 10, PolicyEffect.DENY),
            rule("allow-later", 900, PolicyEffect.ALLOW),
        ),
        facts(),
    )
    assert decision.denied
    assert decision.matched_rule_name == "deny-early"
    assert decision.trace[-1].stopped_evaluation
    # The later rule was never even considered.
    assert all(t.rule_name != "allow-later" for t in decision.trace)


def test_approval_is_sticky_and_cannot_be_downgraded() -> None:
    """Escalation is a floor, not a vote."""
    decision = evaluate(
        bundle(
            rule("escalate", 200, PolicyEffect.REQUIRE_APPROVAL),
            rule("allow", 900, PolicyEffect.ALLOW),
        ),
        facts(),
    )
    assert decision.requires_approval
    assert not decision.allowed


def test_tightest_cap_wins_and_caps_accumulate() -> None:
    decision = evaluate(
        bundle(
            rule("loose", 300, PolicyEffect.CAP, cap_amount_minor=1_000_00),
            rule("tight", 301, PolicyEffect.CAP, cap_amount_minor=250_00),
            rule("allow", 900, PolicyEffect.ALLOW),
        ),
        facts(action_type=ActionType.OFFER_WINBACK_DISCOUNT, amount_minor=800_00),
    )
    assert decision.capped_amount_minor == 250_00
    assert decision.capping_rule_name == "tight"
    assert decision.was_capped
    assert decision.effective_amount == Money(250_00)


def test_a_cap_never_raises_the_proposed_amount() -> None:
    """A ceiling above the request leaves the request alone."""
    decision = evaluate(
        bundle(
            rule("ceiling", 300, PolicyEffect.CAP, cap_amount_minor=5_000_00),
            rule("allow", 900, PolicyEffect.ALLOW),
        ),
        facts(action_type=ActionType.OFFER_WINBACK_DISCOUNT, amount_minor=100_00),
    )
    assert decision.effective_amount == Money(100_00)
    assert not decision.was_capped


def test_percentage_caps_resolve_against_the_subscription_not_the_proposal() -> None:
    """'No more than 15%' means 15% of what the customer pays."""
    decision = evaluate(
        bundle(
            rule("proportionate", 300, PolicyEffect.CAP, cap_percent=15),
            rule("allow", 900, PolicyEffect.ALLOW),
        ),
        facts(
            action_type=ActionType.OFFER_WINBACK_DISCOUNT,
            amount_minor=900_00,
            subscription_mrr_minor=1_000_00,
        ),
    )
    assert decision.capped_amount_minor == 150_00


def test_a_malformed_rule_stops_the_world_rather_than_passing() -> None:
    """'I could not check this constraint' must never read as 'it passed'."""
    broken = bundle(rule("broken", 100, PolicyEffect.ALLOW, {"op": "definitely_not_an_op"}))
    with pytest.raises(MalformedExpression):
        evaluate(broken, facts())


def test_a_rule_naming_an_unknown_fact_is_refused() -> None:
    broken = bundle(
        rule("typo", 100, PolicyEffect.ALLOW, {"op": "eq", "field": "amount_minorr", "value": 1})
    )
    with pytest.raises(MalformedExpression):
        evaluate(broken, facts())


def test_the_trace_records_rules_that_did_not_match() -> None:
    """The useful debugging question is why an expected rule did not fire."""
    decision = evaluate(
        bundle(
            rule("quiet", 100, PolicyEffect.DENY, NEVER),
            rule("allow", 900, PolicyEffect.ALLOW),
        ),
        facts(),
    )
    names = {t.rule_name: t.matched for t in decision.trace}
    assert names == {"quiet": False, "allow": True}


def test_evaluation_is_deterministic() -> None:
    b = default_bundle()
    f = facts(consent_state=ConsentState.GRANTED, is_outreach=True, local_hour_ist=11)
    first, second = evaluate(b, f), evaluate(b, f)
    assert first.effect is second.effect
    assert first.trace_json() == second.trace_json()


@given(
    amount=st.integers(0, 50_000_00),
    hour=st.integers(0, 23),
    contacts=st.integers(0, 10),
    tenure=st.integers(0, 2000),
)
@settings(max_examples=200, deadline=None)
def test_evaluation_is_total_over_the_default_bundle(
    amount: int, hour: int, contacts: int, tenure: int
) -> None:
    """Whatever the facts, the default bundle produces a decision and never raises."""
    b = default_bundle()
    for action in ActionType:
        decision = evaluate(
            b,
            facts(
                action_type=action,
                amount_minor=amount,
                local_hour_ist=hour,
                contacts_last_24h=contacts,
                customer_tenure_days=tenure,
            ),
        )
        assert decision.effect in set(PolicyEffect)


# ---------------------------------------------------------------------------
# The default bundle denies what it claims to deny
# ---------------------------------------------------------------------------

DEFAULT = default_bundle()


def outreach(**kwargs: Any) -> PolicyFacts:
    base: dict[str, Any] = {
        "action_type": ActionType.SEND_REMINDER,
        "is_outreach": True,
        "purpose": MessagePurpose.PAYMENT_RECOVERY_OUTREACH,
        "consent_state": ConsentState.GRANTED,
        "local_hour_ist": 11,
        "merchant_review_first": False,
    }
    base.update(kwargs)
    return PolicyFacts(**base)


def test_withdrawn_consent_blocks_outreach() -> None:
    decision = evaluate(DEFAULT, outreach(consent_state=ConsentState.WITHDRAWN))
    assert decision.denied
    assert decision.matched_rule_name == "consent-withdrawn-blocks-outreach"


def test_absent_consent_blocks_outreach() -> None:
    decision = evaluate(DEFAULT, outreach(consent_state=ConsentState.NEVER_GRANTED))
    assert decision.denied


def test_quiet_hours_block_ordinary_outreach() -> None:
    assert evaluate(DEFAULT, outreach(local_hour_ist=QUIET_HOURS_START + 1)).denied
    assert evaluate(DEFAULT, outreach(local_hour_ist=3)).denied
    assert not evaluate(DEFAULT, outreach(local_hour_ist=11)).denied


def test_step_up_authentication_is_exempt_from_quiet_hours() -> None:
    """The customer is waiting on it in real time. Nothing else gets this exemption."""
    decision = evaluate(
        DEFAULT,
        outreach(local_hour_ist=23, purpose=MessagePurpose.STEP_UP_AUTHENTICATION),
    )
    assert not decision.denied


def test_frequency_cap_blocks_the_next_contact() -> None:
    decision = evaluate(DEFAULT, outreach(contacts_last_24h=MAX_CONTACTS_24H))
    assert decision.denied
    assert decision.matched_rule_name == "contact-frequency-24h"


def test_a_risk_decline_is_never_retried() -> None:
    decision = evaluate(
        DEFAULT,
        facts(
            action_type=ActionType.RETRY_DEBIT,
            is_debit_retry=True,
            is_money_movement=True,
            failure_class=FailureClass.RISK_DECLINED,
            authorisation_decision=AuthorisationDecision.AUTHORISED,
            merchant_review_first=False,
        ),
    )
    assert decision.denied
    assert decision.matched_rule_name == "never-retry-a-risk-decline"


@pytest.mark.parametrize(
    "failure_class",
    [
        FailureClass.INSTRUMENT_EXPIRED,
        FailureClass.MANDATE_REVOKED,
        FailureClass.ACCOUNT_CLOSED,
    ],
)
def test_terminal_failures_are_never_retried(failure_class: FailureClass) -> None:
    decision = evaluate(
        DEFAULT,
        facts(
            action_type=ActionType.RETRY_DEBIT,
            is_debit_retry=True,
            is_money_movement=True,
            failure_class=failure_class,
            authorisation_decision=AuthorisationDecision.AUTHORISED,
            merchant_review_first=False,
        ),
    )
    assert decision.denied


def test_money_never_moves_without_authorisation() -> None:
    decision = evaluate(
        DEFAULT,
        facts(
            action_type=ActionType.RETRY_DEBIT,
            is_debit_retry=True,
            is_money_movement=True,
            failure_class=FailureClass.INSUFFICIENT_FUNDS,
            authorisation_decision=AuthorisationDecision.DENIED,
            merchant_review_first=False,
        ),
    )
    assert decision.denied
    assert decision.matched_rule_name == "unauthorised-actions-never-execute"


def test_review_first_escalates_everything() -> None:
    """The mode every merchant starts in."""
    decision = evaluate(DEFAULT, outreach(merchant_review_first=True))
    assert decision.requires_approval


def test_large_actions_escalate_even_with_review_first_off() -> None:
    decision = evaluate(
        DEFAULT,
        facts(
            action_type=ActionType.OFFER_WINBACK_DISCOUNT,
            is_concession=True,
            consent_state=ConsentState.GRANTED,
            purpose=MessagePurpose.PROMOTIONAL_WINBACK,
            local_hour_ist=11,
            amount_minor=APPROVAL_THRESHOLD_MINOR,
            subscription_mrr_minor=100_000_00,
            budget_headroom_minor=10_000_000,
            customer_concession_headroom_minor=10_000_000,
            customer_tenure_days=800,
            merchant_review_first=False,
        ),
    )
    assert decision.requires_approval


def test_concessions_are_capped_by_both_ceilings() -> None:
    """The absolute rupee ceiling and the proportion of MRR both apply."""
    decision = evaluate(
        DEFAULT,
        facts(
            action_type=ActionType.OFFER_WINBACK_DISCOUNT,
            is_concession=True,
            consent_state=ConsentState.GRANTED,
            purpose=MessagePurpose.PROMOTIONAL_WINBACK,
            local_hour_ist=11,
            amount_minor=4_000_00,
            subscription_mrr_minor=1_000_00,
            budget_headroom_minor=100_000_00,
            customer_concession_headroom_minor=100_000_00,
            customer_tenure_days=800,
            merchant_review_first=False,
        ),
    )
    # 25% of 1,000.00 is 250.00, which is tighter than the 2,000.00 absolute cap.
    expected = min(MAX_CONCESSION_MINOR, 1_000_00 * MAX_CONCESSION_PERCENT_OF_MRR // 100)
    assert decision.capped_amount_minor == expected


def test_anvil_may_always_stop() -> None:
    """Choosing to do nothing further is never blocked."""
    decision = evaluate(
        DEFAULT,
        facts(
            action_type=ActionType.ESCALATE_TO_HUMAN,
            is_terminal_action=True,
            merchant_review_first=True,
        ),
    )
    assert not decision.denied


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def test_hash_ignores_insertion_order() -> None:
    a = [rule("x", 100, PolicyEffect.ALLOW), rule("y", 200, PolicyEffect.DENY)]
    assert bundle_hash(a) == bundle_hash(list(reversed(a)))


def test_hash_changes_when_behaviour_changes() -> None:
    a = [rule("x", 100, PolicyEffect.ALLOW)]
    b = [rule("x", 100, PolicyEffect.DENY)]
    assert bundle_hash(a) != bundle_hash(b)


def test_hash_changes_when_a_description_stops_matching_its_rule() -> None:
    """A description that no longer describes the rule is a real change."""
    a = [rule("x", 100, PolicyEffect.ALLOW)]
    b = [
        CompiledRule(
            id="prl_x",
            name="x",
            priority=100,
            effect=PolicyEffect.ALLOW,
            conditions=ALWAYS,
            description="something else entirely",
        )
    ]
    assert bundle_hash(a) != bundle_hash(b)


# ---------------------------------------------------------------------------
# The compiler
# ---------------------------------------------------------------------------


def proposal(**kwargs: Any) -> ProposedRule:
    base: dict[str, Any] = {
        "name": "merchant-rule",
        "priority": 500,
        "effect": PolicyEffect.DENY,
        "conditions": {"op": "gte", "field": "amount_minor", "value": 100},
        "description": "a rule the merchant asked for",
    }
    base.update(kwargs)
    return ProposedRule(**base)


def carried_immutables() -> list[ProposedRule]:
    return [
        ProposedRule(
            name=r.name,
            priority=r.priority,
            effect=r.effect,
            conditions=r.conditions,
            description=r.description or "",
            cap_amount_minor=r.cap_amount_minor,
            cap_percent=r.cap_percent,
        )
        for r in DEFAULT.immutable_rules
    ]


def test_a_proposal_that_drops_a_regulatory_rule_is_refused() -> None:
    """A merchant cannot consent away a customer's rights on their behalf."""
    with pytest.raises(PolicyCompilationRejected) as caught:
        assemble([proposal()], active=DEFAULT, prose="stop contacting people so much")
    assert any("cannot be removed" in p for p in caught.value.problems)


def test_a_proposal_that_rewrites_a_regulatory_rule_is_refused() -> None:
    """Keeping the name while changing the behaviour must not slip through."""
    tampered = carried_immutables()
    for i, r in enumerate(tampered):
        if r.name == "quiet-hours":
            tampered[i] = ProposedRule(
                name="quiet-hours",
                priority=r.priority,
                effect=PolicyEffect.ALLOW,
                conditions=r.conditions,
                description=r.description,
            )
    with pytest.raises(PolicyCompilationRejected) as caught:
        assemble(tampered, active=DEFAULT, prose="loosen quiet hours")
    assert any("was modified" in p for p in caught.value.problems)


def test_a_proposal_naming_an_unknown_fact_is_refused() -> None:
    bad = proposal(conditions={"op": "gte", "field": "customer_vibes", "value": 5})
    with pytest.raises(PolicyCompilationRejected) as caught:
        assemble([bad, *carried_immutables()], active=DEFAULT, prose="x")
    assert any("customer_vibes" in p for p in caught.value.problems)


def test_a_proposal_in_the_reserved_priority_band_is_refused() -> None:
    with pytest.raises(PolicyCompilationRejected) as caught:
        assemble([proposal(priority=5), *carried_immutables()], active=DEFAULT, prose="x")
    assert any("reserved band" in p for p in caught.value.problems)


def test_a_rule_with_no_description_is_refused() -> None:
    with pytest.raises(PolicyCompilationRejected) as caught:
        assemble([proposal(description="  "), *carried_immutables()], active=DEFAULT, prose="x")
    assert any("not meaningfully approving" in p for p in caught.value.problems)


def test_a_cap_with_no_ceiling_is_refused() -> None:
    bad = proposal(effect=PolicyEffect.CAP)
    problems = verify([bad], active=None)
    assert any("names no ceiling" in p for p in problems)


def test_every_problem_is_reported_at_once() -> None:
    """A merchant should not have to play twenty questions with their own policy."""
    bad = proposal(priority=5, description="", conditions={"op": "eq", "field": "nope", "value": 1})
    problems = verify([bad], active=None)
    assert len(problems) >= 3


def test_a_valid_proposal_compiles_and_carries_the_floor_through() -> None:
    result = assemble(
        [proposal(), *carried_immutables()],
        active=DEFAULT,
        prose="never act on anything under a rupee",
    )
    assert result.bundle.version == DEFAULT.version + 1
    assert result.requires_review
    names = {r.name for r in result.bundle.rules}
    assert immutable_rule_names() <= names
    assert "merchant-rule" in names


def test_the_diff_is_readable_and_names_what_changed() -> None:
    result = assemble([proposal(), *carried_immutables()], active=DEFAULT, prose="x")
    summary = result.diff.summary()
    assert "merchant-rule" in summary
    rendered = result.diff.render()
    assert "+ merchant-rule" in rendered


def test_an_identical_recompilation_reports_no_behavioural_change() -> None:
    same = [
        ProposedRule(
            name=r.name,
            priority=r.priority,
            effect=r.effect,
            conditions=r.conditions,
            description=r.description or "",
            cap_amount_minor=r.cap_amount_minor,
            cap_percent=r.cap_percent,
        )
        for r in DEFAULT.rules
    ]
    d = diff(DEFAULT, assemble(same, active=DEFAULT, prose="x").bundle.rules, 2)
    assert d.is_empty
    assert "No behavioural change" in d.summary()


async def test_compile_policy_refuses_empty_prose() -> None:
    class Stub:
        async def compile_policy(self, **_: Any) -> list[ProposedRule]:
            raise AssertionError("the model must not be called for empty prose")

    with pytest.raises(PolicyCompilationRejected):
        await compile_policy(Stub(), prose="   ", active=DEFAULT)


async def test_compile_policy_runs_the_model_output_through_verification() -> None:
    """The model proposes; verification disposes."""

    class Stub:
        async def compile_policy(self, **_: Any) -> list[ProposedRule]:
            return [proposal(priority=1)]  # inside the reserved band

    with pytest.raises(PolicyCompilationRejected):
        await compile_policy(Stub(), prose="do something clever", active=DEFAULT)
