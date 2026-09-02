"""The policy every merchant starts with.

A new merchant must be safe before they have written a single rule, so the
default bundle is not a permissive placeholder -- it is a working, opinionated
policy that a cautious payments team would recognise. Everything the compiler
later produces is a modification of this.

Rules are grouped by priority band, and the bands are the design:

* **0-99  regulatory and consent floors.** Marked ``is_immutable``. The
  compiler refuses to weaken or remove these however the merchant phrases their
  prose, because a merchant cannot consent away a customer's rights.
* **100-199  hard prohibitions.** Things that are futile or harmful rather than
  illegal -- retrying a revoked mandate, retrying a risk decline.
* **200-299  escalation.** What a human must see.
* **300-399  ceilings.** How much may be conceded.
* **900+  the permits.** Reached only by an action that survived everything
  above it.

Because the evaluator denies on no-match, the permits at the bottom are what
actually make the agent able to act at all. That inversion is intentional: the
readable question a merchant asks of this file is "what is Anvil allowed to
do?", and the answer is a short list at the end rather than an open-ended
absence of prohibitions.
"""

from __future__ import annotations

from typing import Any

from anvil.core.ids import deterministic_id
from anvil.domain.enums import (
    ActionType,
    ConsentState,
    FailureClass,
    MessagePurpose,
    PolicyEffect,
)
from anvil.policy.evaluator import CompiledBundle, CompiledRule
from anvil.policy.hashing import bundle_hash

# --- tunables the console surfaces as plain numbers -------------------------

#: Contacts permitted in a rolling 24 hours and 7 days, per customer.
MAX_CONTACTS_24H = 1
MAX_CONTACTS_7D = 3
#: Minimum hours between two contacts, whatever the window counts say.
MIN_HOURS_BETWEEN_CONTACTS = 20
#: IST hours during which no outreach may be sent.
QUIET_HOURS_START = 21
QUIET_HOURS_END = 8
#: A single action above this needs a human, however well it scores.
APPROVAL_THRESHOLD_MINOR = 5_000_00
#: Ceilings on any one concession.
MAX_CONCESSION_MINOR = 2_000_00
MAX_CONCESSION_PERCENT_OF_MRR = 25
#: Debit attempts permitted against one mandate cycle.
MAX_ATTEMPTS_PER_CYCLE = 4
#: Total contacts on a single case before Anvil stops of its own accord.
MAX_CONTACTS_PER_CASE = 4

#: Failure classes for which a debit retry is refused outright.
TERMINAL_FOR_RETRY: tuple[str, ...] = (
    FailureClass.INSTRUMENT_EXPIRED.value,
    FailureClass.MANDATE_REVOKED.value,
    FailureClass.ACCOUNT_CLOSED.value,
    FailureClass.RISK_DECLINED.value,
)


def _and(*args: dict[str, Any]) -> dict[str, Any]:
    return {"op": "and", "args": list(args)}


def _or(*args: dict[str, Any]) -> dict[str, Any]:
    return {"op": "or", "args": list(args)}


def _eq(field: str, value: Any) -> dict[str, Any]:
    return {"op": "eq", "field": field, "value": value}


def _ne(field: str, value: Any) -> dict[str, Any]:
    return {"op": "ne", "field": field, "value": value}


def _gte(field: str, value: Any) -> dict[str, Any]:
    return {"op": "gte", "field": field, "value": value}


def _gt(field: str, value: Any) -> dict[str, Any]:
    return {"op": "gt", "field": field, "value": value}


def _lt(field: str, value: Any) -> dict[str, Any]:
    return {"op": "lt", "field": field, "value": value}


def _in(field: str, values: list[Any]) -> dict[str, Any]:
    return {"op": "in", "field": field, "value": values}


def _rule(
    name: str,
    priority: int,
    effect: PolicyEffect,
    conditions: dict[str, Any],
    description: str,
    *,
    immutable: bool = False,
    cap_amount_minor: int | None = None,
    cap_percent: int | None = None,
) -> CompiledRule:
    return CompiledRule(
        id=deterministic_id("prl", "default", name),
        name=name,
        priority=priority,
        effect=effect,
        conditions=conditions,
        description=description,
        cap_amount_minor=cap_amount_minor,
        cap_percent=cap_percent,
        is_immutable=immutable,
    )


def default_rules() -> tuple[CompiledRule, ...]:
    """Every rule in the starting bundle, in priority order."""
    return (
        # --- 0-99: regulatory and consent floors, not negotiable -------------
        _rule(
            "consent-withdrawn-blocks-outreach",
            10,
            PolicyEffect.DENY,
            _and(
                _eq("is_outreach", True),
                _in("consent_state", [ConsentState.WITHDRAWN.value, ConsentState.EXPIRED.value]),
            ),
            "The data principal has withdrawn or let lapse their consent for this purpose. "
            "Under the DPDPA no further processing for that purpose is lawful, so the "
            "message is refused rather than merely deprioritised.",
            immutable=True,
        ),
        _rule(
            "no-consent-no-contact",
            11,
            PolicyEffect.DENY,
            _and(
                _eq("is_outreach", True),
                _eq("consent_state", ConsentState.NEVER_GRANTED.value),
            ),
            "No consent receipt exists for this purpose. Consent under the DPDPA is "
            "specific to a purpose and is never inferred from a commercial relationship.",
            immutable=True,
        ),
        _rule(
            "promotional-winback-needs-its-own-consent",
            12,
            PolicyEffect.DENY,
            _and(
                _eq("purpose", MessagePurpose.PROMOTIONAL_WINBACK.value),
                _ne("consent_state", ConsentState.GRANTED.value),
            ),
            "A win-back offer is promotional, not transactional. It requires consent "
            "granted for that specific purpose and cannot ride on a service-message consent.",
            immutable=True,
        ),
        _rule(
            "quiet-hours",
            20,
            PolicyEffect.DENY,
            _and(
                _eq("is_outreach", True),
                _ne("purpose", MessagePurpose.STEP_UP_AUTHENTICATION.value),
                _or(
                    _gte("local_hour_ist", QUIET_HOURS_START),
                    _lt("local_hour_ist", QUIET_HOURS_END),
                ),
            ),
            f"No outreach between {QUIET_HOURS_START}:00 and {QUIET_HOURS_END}:00 IST. "
            "A step-up authentication challenge is exempt because the customer is waiting "
            "on it in real time; nothing else is.",
            immutable=True,
        ),
        _rule(
            "unauthorised-actions-never-execute",
            30,
            PolicyEffect.DENY,
            _and(
                _eq("is_money_movement", True),
                _ne("authorisation_decision", "authorised"),
            ),
            "No money moves without a valid authorisation. The mandate registry is the "
            "authority; this rule ensures a policy misconfiguration cannot bypass it.",
            immutable=True,
        ),
        # --- 100-199: prohibitions that are futile or harmful ----------------
        _rule(
            "never-retry-a-risk-decline",
            110,
            PolicyEffect.DENY,
            _and(
                _eq("is_debit_retry", True),
                _eq("failure_class", FailureClass.RISK_DECLINED.value),
            ),
            "Retrying a risk decline is worse than doing nothing: repeated attempts "
            "degrade the merchant's issuer risk score and can get the descriptor blocked.",
        ),
        _rule(
            "never-retry-a-terminal-failure",
            120,
            PolicyEffect.DENY,
            _and(
                _eq("is_debit_retry", True),
                _in("failure_class", list(TERMINAL_FOR_RETRY)),
            ),
            "An expired card, a revoked mandate and a closed account will all fail again "
            "tomorrow. Every attempt spent here is an attempt not spent on a recoverable case.",
        ),
        _rule(
            "mandate-cycle-attempt-cap",
            130,
            PolicyEffect.DENY,
            _and(
                _eq("is_debit_retry", True),
                _gte("mandate_cycle_attempt_count", MAX_ATTEMPTS_PER_CYCLE),
            ),
            f"No more than {MAX_ATTEMPTS_PER_CYCLE} debit attempts against one mandate cycle.",
        ),
        _rule(
            "contact-frequency-24h",
            140,
            PolicyEffect.DENY,
            _and(
                _eq("is_outreach", True),
                _gte("contacts_last_24h", MAX_CONTACTS_24H),
            ),
            f"At most {MAX_CONTACTS_24H} contact in any rolling 24 hours. Contact pressure "
            "is the largest single driver of churn in the scoring model, so this cap "
            "protects revenue rather than merely being polite.",
        ),
        _rule(
            "contact-frequency-7d",
            141,
            PolicyEffect.DENY,
            _and(
                _eq("is_outreach", True),
                _gte("contacts_last_7d", MAX_CONTACTS_7D),
            ),
            f"At most {MAX_CONTACTS_7D} contacts in any rolling 7 days.",
        ),
        _rule(
            "minimum-gap-between-contacts",
            142,
            PolicyEffect.DENY,
            _and(
                _eq("is_outreach", True),
                _eq("has_prior_contact", True),
                _lt("hours_since_last_contact", MIN_HOURS_BETWEEN_CONTACTS),
            ),
            f"At least {MIN_HOURS_BETWEEN_CONTACTS} hours between two contacts, whatever "
            "the rolling window counts allow.",
        ),
        _rule(
            "stop-after-enough-contacts-on-one-case",
            150,
            PolicyEffect.DENY,
            _and(
                _eq("is_outreach", True),
                _gte("case_contact_count", MAX_CONTACTS_PER_CASE),
            ),
            f"Anvil stops after {MAX_CONTACTS_PER_CASE} contacts on a single case. A "
            "stopping rule the agent can talk itself out of is not a stopping rule.",
        ),
        _rule(
            "concession-must-not-exceed-the-budget",
            160,
            PolicyEffect.DENY,
            _and(
                _eq("is_concession", True),
                _eq("concession_exceeds_budget_headroom", True),
            ),
            "The merchant's authorised concession budget has no room for this. The ledger "
            "enforces the same limit; this rule refuses before a hold is even attempted.",
        ),
        _rule(
            "concession-must-not-exceed-the-customer-ceiling",
            161,
            PolicyEffect.DENY,
            _and(
                _eq("is_concession", True),
                _eq("concession_exceeds_customer_ceiling", True),
            ),
            "This customer has already received their full concession allowance.",
        ),
        # --- 200-299: escalation to a human ----------------------------------
        _rule(
            "review-first-merchants-approve-everything",
            210,
            PolicyEffect.REQUIRE_APPROVAL,
            _and(_eq("merchant_review_first", True), _eq("is_terminal_action", False)),
            "This merchant is in review-first mode, so every action is drafted for a "
            "human rather than executed. This is the default a merchant starts in.",
        ),
        _rule(
            "large-actions-need-a-human",
            220,
            PolicyEffect.REQUIRE_APPROVAL,
            _gte("amount_minor", APPROVAL_THRESHOLD_MINOR),
            f"Any single action at or above "
            f"{APPROVAL_THRESHOLD_MINOR // 100:,} rupees is reviewed by a person, "
            "however confident the model is.",
        ),
        _rule(
            "every-concession-is-reviewed-for-new-customers",
            230,
            PolicyEffect.REQUIRE_APPROVAL,
            _and(_eq("is_concession", True), _lt("customer_tenure_days", 60)),
            "Conceding to a customer of under two months is reviewed by a person: there "
            "is not yet enough history to tell a genuine payment problem from abuse.",
        ),
        _rule(
            "repeat-concessions-are-reviewed",
            231,
            PolicyEffect.REQUIRE_APPROVAL,
            _and(_eq("is_concession", True), _gte("prior_concession_count", 2)),
            "A third concession to the same customer is a commercial decision about the "
            "relationship, not a recovery tactic, so a person makes it.",
        ),
        _rule(
            "writing-off-money-is-a-human-decision",
            240,
            PolicyEffect.REQUIRE_APPROVAL,
            _eq("action_type", ActionType.STOP_AND_WRITE_OFF.value),
            "Abandoning a receivable is a decision about the merchant's money and is "
            "never taken unattended.",
        ),
        _rule(
            "low-confidence-cases-are-reviewed",
            250,
            PolicyEffect.REQUIRE_APPROVAL,
            _and(_eq("is_money_movement", True), _lt("recovery_likelihood", 150)),
            "When the scheduler itself puts recovery below 15%, a person decides whether "
            "spending another attempt is worth it.",
        ),
        # --- 300-399: ceilings ------------------------------------------------
        _rule(
            "concession-absolute-ceiling",
            310,
            PolicyEffect.CAP,
            _eq("is_concession", True),
            f"No single concession may exceed {MAX_CONCESSION_MINOR // 100:,} rupees.",
            cap_amount_minor=MAX_CONCESSION_MINOR,
        ),
        _rule(
            "concession-proportionate-to-the-subscription",
            311,
            PolicyEffect.CAP,
            _eq("is_concession", True),
            f"No concession may exceed {MAX_CONCESSION_PERCENT_OF_MRR}% of what the "
            "customer actually pays each month. Conceding more than that to save a "
            "subscription destroys the value it was meant to protect.",
            cap_percent=MAX_CONCESSION_PERCENT_OF_MRR,
        ),
        # --- 900+: what Anvil is actually permitted to do ---------------------
        _rule(
            "permit-outreach",
            910,
            PolicyEffect.ALLOW,
            _and(_eq("is_outreach", True), _eq("consent_state", ConsentState.GRANTED.value)),
            "Contacting a consenting customer about a payment that failed is permitted.",
        ),
        _rule(
            "permit-authorised-debit-retries",
            920,
            PolicyEffect.ALLOW,
            _and(
                _eq("is_debit_retry", True),
                _eq("authorisation_decision", "authorised"),
            ),
            "Retrying a debit against a valid mandate, for a failure class worth "
            "retrying, is the core recovery action and is permitted.",
        ),
        _rule(
            "permit-instrument-and-mandate-repair",
            930,
            PolicyEffect.ALLOW,
            _in(
                "action_type",
                [
                    ActionType.REQUEST_INSTRUMENT_UPDATE.value,
                    ActionType.REQUEST_MANDATE_REAUTH.value,
                    ActionType.TRIGGER_STEP_UP.value,
                    ActionType.SEND_PAYMENT_LINK.value,
                ],
            ),
            "Asking a customer to fix the underlying problem moves no money and is the "
            "only recovery path for the terminal failure classes.",
        ),
        _rule(
            "permit-bounded-concessions",
            940,
            PolicyEffect.ALLOW,
            _eq("is_concession", True),
            "Conceding within the authorised budget and the ceilings above is permitted. "
            "This is what 'bounded authority' means in practice.",
        ),
        _rule(
            "permit-stopping",
            950,
            PolicyEffect.ALLOW,
            _eq("is_terminal_action", True),
            "Anvil may always stop, escalate or close a case. Choosing to do nothing "
            "further is never blocked by policy.",
        ),
    )


def default_bundle(bundle_id: str = "pol_default", version: int = 1) -> CompiledBundle:
    """The starting bundle, validated and content-addressed."""
    rules = default_rules()
    bundle = CompiledBundle(
        id=bundle_id, version=version, rules=rules, content_hash=bundle_hash(rules)
    )
    bundle.validate()
    return bundle


def immutable_rule_names() -> frozenset[str]:
    """Names the compiler may never drop or weaken."""
    return frozenset(r.name for r in default_rules() if r.is_immutable)
