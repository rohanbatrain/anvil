# Anvil — Policy, authorisation and consent

Three deterministic gates stand between a proposed action and a rupee moving. None of them contains a
model, and each fails closed:

| Gate | Question | Code | Failure mode |
| --- | --- | --- | --- |
| **Mandate authorisation** | Does the right to do this exist? | `anvil/mandates/authorise.py` | `DENIED`, or `REQUIRES_STEP_UP` when the principal can be asked |
| **Policy** | Is exercising it permitted, and how much of it? | `anvil/policy/` | `DENY` — and an action no rule permits is denied |
| **Consent and frequency** | May we put this message in front of this person, now? | `anvil/channels/` | suppression, persisted with its reason |

The order matters and is fixed in the recovery graph (`anvil/graph/nodes/gate.py`):
`authorise → (step-up) → policy → (approval) → execute`. Authorisation runs first because it is the only
check that answers "does this right exist?" rather than "should we exercise it?". Consent and frequency
run again at the channel boundary, on the facts as they are at send time rather than at plan time.

This document is the reference for all three. It describes what the code does today; every example in it
is real output from the shipped default bundle, the tests, or `scripts/tour.py`.

---

## 1. The expression language

A policy rule's condition is a JSON expression tree — a tree of tagged objects, not code. It cannot call,
loop, import, allocate unboundedly, or see anything except the fact catalogue. Evaluation is pure, total
and side-effect free, which is what lets a persisted `PolicyEvaluation` be replayed rather than merely
believed.

```json
{"op": "and", "args": [
  {"op": "eq", "field": "is_outreach", "value": true},
  {"op": "in", "field": "consent_state", "value": ["withdrawn", "expired"]}
]}
```

### 1.1 Operators

The set is closed. There are fourteen operators and no way to add a fifteenth from data.

| Operator | Node shape | Operand | Meaning |
| --- | --- | --- | --- |
| `and` | `{"op": "and", "args": [...]}` | non-empty list of nodes | every operand is true |
| `or` | `{"op": "or", "args": [...]}` | non-empty list of nodes | some operand is true |
| `not` | `{"op": "not", "arg": {...}}` | one node | negation |
| `always` | `{"op": "always"}` | — | true. The honest way to write a blanket rule |
| `never` | `{"op": "never"}` | — | false. Parks a rule without deleting its history |
| `eq` | `{"op": "eq", "field": F, "value": V}` | any fact kind | equality |
| `ne` | `{"op": "ne", "field": F, "value": V}` | any fact kind | inequality |
| `lt` | `{"op": "lt", "field": F, "value": N}` | integer facts only | `fact < N` |
| `lte` | `{"op": "lte", "field": F, "value": N}` | integer facts only | `fact <= N` |
| `gt` | `{"op": "gt", "field": F, "value": N}` | integer facts only | `fact > N` |
| `gte` | `{"op": "gte", "field": F, "value": N}` | integer facts only | `fact >= N` |
| `in` | `{"op": "in", "field": F, "value": [...]}` | non-empty list of literals | membership |
| `not_in` | `{"op": "not_in", "field": F, "value": [...]}` | non-empty list of literals | non-membership |
| `between` | `{"op": "between", "field": F, "value": [low, high]}` | integer facts only, `low <= high` | `low <= fact <= high`, both ends inclusive |

There is no field-to-field comparison. The language compares a fact to a literal and nothing else; where a
rule needs a relation between two facts ("is this concession bigger than the remaining budget?"), the
relation is precomputed as its own boolean fact so it is visible in the persisted row.

### 1.2 What a well-formed tree must satisfy

`validate_expression()` runs at rule construction, at compile time, and again at the top of every
evaluation. Trees are tiny, so validating three times costs nothing and removes the possibility of an
unvalidated tree reaching the interpreter by some route nobody thought of.

- The node is an object whose keys are strings and are **exactly** the set the operator takes — no extras,
  no omissions.
- `op` is one of the fourteen above.
- `field` names a fact in the catalogue.
- The literal matches the fact's declared kind: `true`/`false` for a boolean fact, an integer (and not a
  boolean) for an integer fact, a string for a string fact.
- A string fact with a closed vocabulary checks membership; an integer fact checks its declared minimum
  and maximum.
- `null` is permitted only for the two nullable facts, `failure_class` and `purpose`.
- **Floats are refused everywhere.** Money is integer minor units; a float in a rule is a defect, not a
  rounding preference.
- Ordered and range operators require an integer fact.
- The tree is at most **12** deep and **256** nodes in total.

Anything else raises `MalformedExpression` (a `ValidationError`, code `malformed_policy_expression`)
carrying a JSON path to the offending node, because the first question a merchant asks about a rejected
policy is *which bit*.

The tempting alternative — treat anything unparseable as "did not match" — is catastrophic in a
fail-closed system: a typo in a DENY rule would silently turn it off, and the result is indistinguishable
from a rule that was correctly evaluated and correctly did not fire. Callers must treat an exception as a
denial plus an alarm, never as a pass.

Real refusals:

| Written | Message |
| --- | --- |
| `{"op": "eq", "field": "amount_minor", "value": 1499.0}` | `floats are not permitted in a policy expression; use integer minor units (at $.value)` |
| `{"op": "gt", "field": "failure_class", "value": "unknown"}` | `'gt' needs an integer fact; failure_class is string (at $.value)` |
| `{"op": "eq", "field": "merchant_review_first", "value": 1}` | `merchant_review_first compares against true or false (at $.value)` |
| `{"op": "eq", "field": "failure_class", "value": "expired"}` | `'expired' is not a failure_class; allowed values are [...]` |
| `{"op": "eq", "field": "consent_state", "value": null}` | `consent_state is never null (at $.value)` |
| `{"op": "gte", "field": "local_hour_ist", "value": 25}` | `local_hour_ist is never above 23 (at $.value)` |
| `{"op": "between", "field": "amount_minor", "value": [500, 100]}` | `'between' bounds are inverted (at $.value)` |
| `{"op": "in", "field": "failure_class", "value": []}` | `'in' needs a non-empty list of literals (at $.value)` |
| `{"op": "matches", "field": "failure_class", "value": "x"}` | `unknown operator 'matches'; known operators are [...]` |
| `{"op": "eq", "field": "amount_minor", "value": 1, "unit": "paise"}` | `operator 'eq' takes exactly ['field', 'op', 'value'], got ['field', 'op', 'unit', 'value']` |
| `{"op": "and", "args": []}` | `'and' needs a non-empty list of operands` |

### 1.3 Evaluation of one tree

`evaluate_expression(node, facts)` validates, then walks. Three semantics are worth stating:

- **A fact absent from the evaluated fact map raises.** It cannot be absent in practice — the map is
  `PolicyFacts.to_json_dict()`, which always carries every field — but a hand-edited row is caught.
- **`True` is never `1`.** Python's `bool` is an `int`, so a plain `==` would let a boolean fact match a
  numeric literal. Equality compares booleans only to booleans, by identity.
- **`null` compares as a value.** `eq purpose null` is true when no purpose is set; `ne purpose
  'step_up_authentication'` is *true* for a null purpose, which is how the quiet-hours rule catches an
  outreach action that declares no purpose. Ordered comparisons against a null would raise, but no
  nullable fact is an integer, so no valid rule can reach that branch.

### 1.4 Builders and rendering

Hand-writing nested dicts is how typos get into a bundle. `anvil/policy/expressions.py` exports builders
that emit exactly the JSON the compiler emits: `all_of`, `any_of`, `negate`, `always`, `never`, `eq`, `ne`,
`lt`, `lte`, `gt`, `gte`, `is_in`, `not_in`, `between`.

`describe(node)` renders a tree as one line of English. A merchant approving a compiled policy is approving
the tree, not the prose that produced it, so the console, the diff and the evaluation trace all show the
tree read back:

```
(is_outreach == true and purpose != 'step_up_authentication' and (local_hour_ist >= 21 or local_hour_ist < 8))
```

---

## 2. The fact namespace

`anvil/policy/facts.py` defines the *entire* surface a rule may test. Three constraints shape it:

- **Everything is a JSON scalar.** `int`, `bool`, or a closed-vocabulary string. No floats, no nested
  objects, no datetimes — the instant is reduced to the facts a rule actually needs (`local_hour_ist`,
  `hours_since_failure`) so every comparison is a total ordering over integers.
- **There are no optional numbers.** An absent measurement would force ordered comparisons to invent a
  truth value for `None`, and a rule that quietly fails to match is a rule that quietly allows. "Never
  contacted" is the sentinel `8760` hours, paired with an explicit boolean.
- **Relations between facts are precomputed** and stored as their own boolean facts, so a reviewer sees
  them in the persisted row.

`PolicyFacts` is a frozen Pydantic model with `extra="forbid"`: a typo'd fact name is an error at the
boundary, not a silently ignored key. An import-time check asserts the catalogue and the model name exactly
the same fields, so drift is impossible rather than merely unlikely.

### 2.1 The catalogue

36 facts. `Default` is what `PolicyFacts` uses when the caller omits the field.

| Fact | Type | Domain | Default | Source |
| --- | --- | --- | --- | --- |
| `action_type` | string | the 15 `ActionType` members | *required* | the proposed action |
| `amount_minor` | int | `>= 0` | `0` | the proposed action; zero for pure outreach |
| `currency` | string | `INR`, `USD` | `INR` | case currency |
| `is_money_movement` | boolean | — | derived | `ActionType.moves_money` |
| `is_concession` | boolean | — | derived | `ActionType.is_concession` |
| `is_outreach` | boolean | — | derived | membership of `OUTREACH_ACTIONS` |
| `is_debit_retry` | boolean | — | derived | membership of `DEBIT_RETRY_ACTIONS` |
| `is_terminal_action` | boolean | — | derived | `ActionType.is_terminal` |
| `failure_class` | string, nullable | the 10 `FailureClass` members, or `null` before classification | `null` | the classifier |
| `is_terminal_failure` | boolean | — | derived | failure class whose retry curve posture is `NEVER` |
| `hours_since_failure` | int | `>= 0` | `0` | case state |
| `case_attempt_count` | int | `>= 0` | `0` | recovery attempts already made on this case |
| `mandate_cycle_attempt_count` | int | `>= 0` | `0` | debit attempts consumed in the mandate's current cycle |
| `case_contact_count` | int | `>= 0` | `0` | outreach already sent on this case |
| `contacts_last_24h` | int | `>= 0` | `0` | contact ledger |
| `contacts_last_7d` | int | `>= 0` | `0` | contact ledger |
| `hours_since_last_contact` | int | `0..8760` | `8760` | contact ledger; the maximum is the never-contacted sentinel |
| `has_prior_contact` | boolean | — | derived | `hours_since_last_contact < 8760` |
| `local_hour_ist` | int | `0..23` | `12` | `ist_hour(now)` from the injected clock |
| `local_day_of_month_ist` | int | `1..31` | `1` | `ist_day_of_month(now)` |
| `customer_tenure_days` | int | `>= 0` | `0` | customer read model |
| `lifetime_value_minor` | int | `>= 0` | `0` | customer read model |
| `prior_concession_count` | int | `>= 0` | `0` | customer read model |
| `prior_concessions_minor` | int | `>= 0` | `0` | customer read model |
| `customer_concession_headroom_minor` | int | `>= 0` | `0` | per-customer concession ceiling, less what is spent |
| `concession_exceeds_customer_ceiling` | boolean | — | derived | see below |
| `subscription_mrr_minor` | int | `>= 0` | `0` | subscription |
| `concession_percent_of_mrr` | int | `0..100000` | derived | see below |
| `budget_headroom_minor` | int | `>= 0` | `0` | ledger: unreserved room in the merchant's concession budget |
| `concession_exceeds_budget_headroom` | boolean | — | derived | see below |
| `purpose` | string, nullable | the 6 `MessagePurpose` members, or `null` for actions that send nothing | `null` | the action payload |
| `consent_state` | string | `granted`, `withdrawn`, `expired`, `never_granted` | `never_granted` | the consent gate, for exactly that purpose |
| `authorisation_decision` | string | `authorised`, `requires_step_up`, `denied` | `denied` | the mandate registry, written by the `authorise` node |
| `recovery_likelihood` | int | `0..1000` | `0` | risk scoring; P(recovery) in per-mille |
| `churn_risk` | int | `0..1000` | `0` | risk scoring; P(churn) in per-mille |
| `merchant_review_first` | boolean | — | `true` | merchant settings |

Every default is the conservative reading: no consent, no authorisation, review-first on, never contacted.

### 2.2 Derived facts

Ten facts are computed from the others before field validation. Supplying one is allowed — that is what
makes a stored fact row round-trip — but supplying a value that *disagrees* with the computation raises,
so a persisted row can never claim a relation its own components contradict.

| Fact | Computation |
| --- | --- |
| `is_money_movement` | `action_type ∈ {retry_debit, split_debit, offer_partial_payment}` |
| `is_concession` | `action_type ∈ {grant_grace_period, offer_partial_payment, offer_plan_downgrade, offer_winback_discount}` |
| `is_outreach` | `action_type ∈ {request_instrument_update, send_payment_link, request_mandate_reauth, send_reminder, send_dunning_notice, offer_partial_payment, offer_plan_downgrade, offer_winback_discount}` |
| `is_debit_retry` | `action_type ∈ {retry_debit, split_debit}` |
| `is_terminal_action` | `action_type ∈ {escalate_to_human, stop_and_write_off, mark_churned}` |
| `is_terminal_failure` | `failure_class ∈ {instrument_expired, mandate_revoked, account_closed, risk_declined}` — read from the retry curves, not restated |
| `has_prior_contact` | `hours_since_last_contact < 8760` |
| `concession_percent_of_mrr` | `0` if not a concession or the amount is non-positive; `100000` if MRR is zero; otherwise `ceil(amount_minor × 100 / subscription_mrr_minor)`, capped at `100000` |
| `concession_exceeds_budget_headroom` | `is_concession and amount_minor > budget_headroom_minor` |
| `concession_exceeds_customer_ceiling` | `is_concession and amount_minor > customer_concession_headroom_minor` |

`TRIGGER_STEP_UP` is deliberately *not* in `OUTREACH_ACTIONS`. An AFA challenge is authentication the
customer is already waiting on, not outreach; suppressing it overnight would strand a live payment journey
rather than protect anyone from being bothered.

Two sentinels carry meaning:

- `NEVER_CONTACTED_HOURS = 8760` (one year). Longer than any cooling-off window a merchant could sanely
  write, so `hours_since_last_contact < N` is false exactly when it should be.
- `UNPRICED_CONCESSION_PERCENT = 100000`. A concession against a subscription with no recorded price cannot
  be justified as a fraction of nothing, so the ratio reads as effectively unbounded and every percentage
  ceiling trips — rather than a zero that would wave it through.

### 2.3 Where the facts come from at runtime

`_facts_for()` in `anvil/graph/nodes/gate.py` assembles the map from graph state, the proposed action and
the clock. Only facts Anvil observed itself; nothing the model wrote reaches a comparison. Two mappings are
worth knowing when reading a stored evaluation: the graph currently supplies `hours_since_failure` from the
state's `hours_since_last_contact`, and supplies `mandate_cycle_attempt_count` from the same
`attempts_made` counter it uses for `case_attempt_count`. The authorisation usage row, not the fact, is the
authority on how much of a mandate cycle has actually been spent (§7.6).

The same fact model is the request body of `POST /policy/evaluate`, so an arbitrary fact set can be
evaluated against the live bundle and a typo is a 422 at the boundary.

---

## 3. Compilation and content hashing

### 3.1 The pipeline

The compiler (`anvil/policy/compiler.py`) is the one place a language model touches policy, and the shape of
that contact is the point: **the model authors the policy, it never is the policy.** Compilation happens
once, at build time, under human review. After that the compiled artifact executes with no model anywhere
near it, so the thousandth evaluation is identical to the first.

1. **Propose.** The model reads merchant prose and emits `ProposedRule`s in the same expression-tree shape
   the validator accepts. It is reached through a narrow `PolicyCompilerModel` protocol declared by the
   policy module itself — the compiler cannot ask a model anything except "turn this prose into candidate
   rules".
2. **Verify.** Structure, fact names, priority band, cap sanity, descriptions, and the immutable floor.
3. **Diff.** Rules added, removed and changed, with conditions rendered back as English.
4. **Approve.** Only a human activates a bundle. Until then it is `PROPOSED` and the evaluator will not load
   it. `CompilationResult.requires_review` is a property that is always `True`, stated as a property so no
   caller can assume otherwise.

`compile_policy()` refuses empty prose without calling the model at all.

### 3.2 What verification refuses

Every problem is reported at once. A merchant who fixes one problem, resubmits, and discovers a second is
being made to play twenty questions with their own policy.

| Condition | Message |
| --- | --- |
| Blank name | `a proposed rule has no name` |
| Duplicate name | `'x' is defined twice; rule names must be unique` |
| Malformed condition | `'x' has a malformed condition: <message with path>` |
| Unknown fact (checked only when the tree is otherwise valid) | `'x' tests foo, which is not a fact Anvil records` |
| `priority < 100` and not a floor rule | `'x' claims priority 5, inside the reserved band below 100 where the regulatory floor runs` |
| `CAP` with neither ceiling | `'x' caps something but names no ceiling` |
| `cap_amount_minor <= 0` | `'x' declares a non-positive rupee ceiling` |
| `cap_percent` outside `0..100` | `'x' declares a percentage ceiling outside 0-100` |
| Blank description | `'x' has no description. A merchant approving a rule they cannot read is not meaningfully approving it` |
| An immutable rule is missing | `'quiet-hours' is a regulatory rule and cannot be removed. A merchant cannot consent away a customer's rights on their behalf` |
| An immutable rule is altered | `'quiet-hours' is a regulatory rule and was modified. Immutable rules must be carried through unchanged` |

The immutable check compares **canonical content**, not names, so a proposal cannot keep the name of a
consent rule while rewriting what it does. Priorities below `RESERVED_PRIORITY_CEILING = 100` are reserved
for the floor; the floor's own names are exempt from that check, which is what lets "carry the floor
through" and "stay out of the floor's priorities" both be true at once.

`assemble()` then carries the active bundle's immutable rules through explicitly rather than trusting them
to be present, assigns fresh rule ids, sets `version = active.version + 1`, and validates the result.
Because the new bundle is *the proposal plus the floor*, any non-immutable rule the proposal omits is
removed — the diff says so, in those words.

### 3.3 The content hash

A bundle is content-addressed so that "is this the bundle the merchant approved?" is a byte comparison
rather than a judgement. `anvil/policy/hashing.py`:

```
canonical_rule(r) = {cap_amount_minor, cap_percent, conditions, description,
                     effect, is_immutable, name, priority}
canonical_bundle  = json.dumps(sorted(rules, key=(priority, name)),
                               sort_keys=True, separators=(",", ":"), ensure_ascii=False)
bundle_hash       = blake2b(canonical_bundle, digest_size=32).hexdigest()   # 64 hex chars
```

Two exclusions are deliberate:

- **Ids and timestamps are out**, so a bundle re-imported into a fresh database hashes identically to the
  one it came from. Otherwise the hash would identify a row rather than a policy. Anything not in the eight
  fields above — row ids, created-at stamps, the rule's `currency` column, the bundle's own id and version —
  is not covered.
- **Descriptions are in**, because a rule whose description no longer matches its condition is a rule a
  human will approve on false pretences, and that should register as a change.

Rules are sorted by `(priority, name)` rather than left in list order, so two bundles with the same rules in
a different insertion order hash the same. `canonical_bundle()` is public so that a hash can be explained
rather than merely asserted.

Why it matters for audit: `PolicyEvaluation` stores the bundle id, the bundle version, the matched rule, the
full trace and the exact fact set. With a content-addressed bundle, replaying a months-old decision is
loading the row, re-running the same rules, and comparing — not reasoning about what the policy probably was
at the time.

---

## 4. Evaluation semantics

`evaluate(bundle, facts) -> PolicyDecision` (`anvil/policy/evaluator.py`). Pure, total, and it never raises
on data. The only exception it can raise is `MalformedExpression`, which signals a corrupt bundle rather
than a business outcome — a rule that cannot be evaluated must stop the world, because the alternative is
treating "I could not check this constraint" as "this constraint passed".

Rules run in `(priority, name)` order — lower priority first, name breaking ties, so evaluation order is
total and does not depend on how the rows came back from the database.

### 4.1 The five rules

1. **The first matching DENY wins outright and stops evaluation.** A later ALLOW cannot rescue a denied
   action. Without this, rule ordering becomes a subtle source of permission.
2. **REQUIRE_APPROVAL is sticky.** Once any rule has escalated an action to a human, no subsequent rule can
   drop it back to an unattended ALLOW. Escalation is a floor, not a vote.
3. **The tightest CAP wins, and caps accumulate.** Every matching cap is applied and the smallest survives.
   A bundle with two overlapping ceilings enforces the lower one, which is the only reading that cannot be
   gamed by adding rules.
4. **No match denies.** An action nobody wrote a rule about is refused. This is the single most important
   line in the module: a gap in the policy is a blocked action rather than an unbounded one, so forgetting
   to write a rule is safe.
5. **An empty bundle denies everything.** Stated separately so the reason is unmistakable in a log: *"the
   active policy bundle contains no rules, so nothing is permitted. An empty policy denies everything by
   design; it is not a licence to act."*

### 4.2 The decision

| Field | Meaning |
| --- | --- |
| `effect` | `allow`, `deny`, `require_approval` — `cap` is an effect a rule declares, never a decision |
| `bundle_id`, `bundle_version` | which policy decided |
| `facts` | the exact `PolicyFacts` evaluated |
| `trace` | one `RuleTrace` per rule considered, matched or not |
| `matched_rule_id`, `matched_rule_name` | the rule the decision is attributed to |
| `capped_amount_minor` | `min(tightest ceiling, proposed amount)`, or `None` when no cap matched |
| `capping_rule_name` | which cap rule was tightest |
| `reason` | the matched rule's description, or a fallback naming the rule |
| `approval_reasons` | every escalating rule's description, in order |

Properties: `allowed` is true **only** for an unattended `ALLOW` (approval-required is not allowed yet);
`denied`; `requires_approval`; `effective_amount` (the capped amount if any, else the proposal);
`was_capped` (true only when the cap is strictly below the proposal); `raise_if_denied()` raises
`PolicyDenied` carrying the bundle, version, rule and action type; `trace_json()` for persistence.

Which rule gets named:

- **DENY** — the rule that denied. Evaluation stops there, and its trace entry carries
  `stopped_evaluation: true`.
- **REQUIRE_APPROVAL** — the *last* matching approval rule in priority order; `reason` is every approval
  reason joined with `; `, and `approval_reasons` keeps them separately.
- **ALLOW** — the *first* matching ALLOW rule. A concession that is also outreach is therefore attributed to
  `permit-outreach` (priority 910) rather than `permit-bounded-concessions` (940).

### 4.3 Caps

`CompiledRule.ceiling_for(facts)` resolves a rule's ceiling:

- `cap_amount_minor` is an absolute rupee ceiling.
- `cap_percent` resolves against **`subscription_mrr_minor`**, not against the proposed amount, because "no
  more than 15%" in a merchant's head means 15% of what the customer pays, not 15% of whatever the agent
  happened to propose. When MRR is zero the percentage cap contributes no ceiling.
- A rule declaring both contributes the tighter of the two.

Across rules, the smallest ceiling wins, and the final amount is `min(ceiling, proposed)` — a cap never
raises a proposal. A `CAP` rule must declare at least one ceiling: `verify()` refuses one that does not, and
`CompiledRule.validate()` also refuses it, though it currently surfaces as a `TypeError` rather than a
`MalformedExpression`.

### 4.4 The trace

Non-matching rules are recorded too, because the useful debugging question is almost always "why did the
rule I expected to fire *not* fire?", and a trace that only lists matches cannot answer it. The full trace
for a reminder attempted at 23:00 IST:

```json
[{"rule_name": "consent-withdrawn-blocks-outreach", "priority": 10, "effect": "deny", "matched": false,
  "condition": "(is_outreach == true and consent_state in ['withdrawn', 'expired'])"},
 {"rule_name": "no-consent-no-contact", "priority": 11, "effect": "deny", "matched": false,
  "condition": "(is_outreach == true and consent_state == 'never_granted')"},
 {"rule_name": "promotional-winback-needs-its-own-consent", "priority": 12, "effect": "deny", "matched": false,
  "condition": "(purpose == 'promotional_winback' and consent_state != 'granted')"},
 {"rule_name": "quiet-hours", "priority": 20, "effect": "deny", "matched": true, "stopped_evaluation": true,
  "condition": "(is_outreach == true and purpose != 'step_up_authentication' and (local_hour_ist >= 21 or local_hour_ist < 8))"}]
```

Four rules considered, one matched, evaluation stopped. An allowed action's trace has all 27.

---

## 5. The default bundle

Every merchant starts here. It is not a permissive placeholder; it is a working, opinionated policy a
cautious payments team would recognise. As shipped: **27 rules, 5 of them immutable**, content hash
`7023ab4499ea6cce22a980b4dbf5e781d9e4f9136c181a5aafa6f75702b69083`.

Rules are grouped by priority band, and the bands are the design:

| Band | Purpose |
| --- | --- |
| 0–99 | Regulatory and consent floors. `is_immutable`; the compiler refuses to weaken or remove them |
| 100–199 | Hard prohibitions — futile or harmful rather than illegal |
| 200–299 | Escalation: what a human must see |
| 300–399 | Ceilings: how much may be conceded |
| 900+ | The permits. Reached only by an action that survived everything above |

Because the evaluator denies on no-match, the permits at the bottom are what make the agent able to act at
all. That inversion is intentional: the readable question is "what is Anvil allowed to do?", and the answer
is a short list at the end rather than an open-ended absence of prohibitions.

### 5.1 Tunables

Plain numbers in `anvil/policy/defaults.py`, surfaced by the console.

| Constant | Value | Used by |
| --- | --- | --- |
| `MAX_CONTACTS_24H` | 1 | `contact-frequency-24h` |
| `MAX_CONTACTS_7D` | 3 | `contact-frequency-7d` |
| `MIN_HOURS_BETWEEN_CONTACTS` | 20 | `minimum-gap-between-contacts` |
| `QUIET_HOURS_START` / `QUIET_HOURS_END` | 21 / 8 IST | `quiet-hours` |
| `APPROVAL_THRESHOLD_MINOR` | ₹5,000.00 | `large-actions-need-a-human` |
| `MAX_CONCESSION_MINOR` | ₹2,000.00 | `concession-absolute-ceiling` |
| `MAX_CONCESSION_PERCENT_OF_MRR` | 25 | `concession-proportionate-to-the-subscription` |
| `MAX_ATTEMPTS_PER_CYCLE` | 4 | `mandate-cycle-attempt-cap` |
| `MAX_CONTACTS_PER_CASE` | 4 | `stop-after-enough-contacts-on-one-case` |
| `TERMINAL_FOR_RETRY` | `instrument_expired`, `mandate_revoked`, `account_closed`, `risk_declined` | `never-retry-a-terminal-failure` |

### 5.2 Band 0–99 — the regulatory floor (immutable)

| # | Rule | Condition | Why |
| --- | --- | --- | --- |
| 10 | `consent-withdrawn-blocks-outreach` | `is_outreach == true and consent_state in ['withdrawn', 'expired']` | The data principal has withdrawn or let lapse consent for this purpose. Under the DPDPA no further processing for that purpose is lawful, so the message is refused rather than deprioritised |
| 11 | `no-consent-no-contact` | `is_outreach == true and consent_state == 'never_granted'` | Consent is specific to a purpose and is never inferred from a commercial relationship |
| 12 | `promotional-winback-needs-its-own-consent` | `purpose == 'promotional_winback' and consent_state != 'granted'` | A win-back offer is promotional. It cannot ride on a service-message consent. Note this keys on the **purpose**, not the action type |
| 20 | `quiet-hours` | `is_outreach == true and purpose != 'step_up_authentication' and (local_hour_ist >= 21 or local_hour_ist < 8)` | No outreach 21:00–08:00 IST. Step-up is exempt because the customer is waiting on it in real time; nothing else is |
| 30 | `unauthorised-actions-never-execute` | `is_money_movement == true and authorisation_decision != 'authorised'` | No money moves without a valid authorisation. The mandate registry is the authority; this rule ensures a policy misconfiguration cannot bypass it |

Rule 30 is why every action passes through the `authorise` node even when it moves no money: the
authorisation result is a *fact the policy engine reads*, and an absent value would make this rule silently
vacuous.

### 5.3 Band 100–199 — hard prohibitions

| # | Rule | Condition | Why |
| --- | --- | --- | --- |
| 110 | `never-retry-a-risk-decline` | `is_debit_retry == true and failure_class == 'risk_declined'` | Repeated attempts degrade the merchant's issuer risk score and can get the descriptor blocked. Worse than doing nothing |
| 120 | `never-retry-a-terminal-failure` | `is_debit_retry == true and failure_class in ['instrument_expired', 'mandate_revoked', 'account_closed', 'risk_declined']` | These fail again tomorrow. Every attempt spent here is an attempt not spent on a recoverable case |
| 130 | `mandate-cycle-attempt-cap` | `is_debit_retry == true and mandate_cycle_attempt_count >= 4` | No more than four debit attempts against one mandate cycle |
| 140 | `contact-frequency-24h` | `is_outreach == true and contacts_last_24h >= 1` | Contact pressure is the largest single driver of churn in the scoring model, so this cap protects revenue rather than merely being polite |
| 141 | `contact-frequency-7d` | `is_outreach == true and contacts_last_7d >= 3` | At most three contacts in any rolling seven days |
| 142 | `minimum-gap-between-contacts` | `is_outreach == true and has_prior_contact == true and hours_since_last_contact < 20` | At least 20 hours between two contacts, whatever the window counts allow |
| 150 | `stop-after-enough-contacts-on-one-case` | `is_outreach == true and case_contact_count >= 4` | Anvil stops after four contacts on one case. A stopping rule the agent can talk itself out of is not a stopping rule |
| 160 | `concession-must-not-exceed-the-budget` | `is_concession == true and concession_exceeds_budget_headroom == true` | The ledger enforces the same limit; this refuses before a hold is even attempted |
| 161 | `concession-must-not-exceed-the-customer-ceiling` | `is_concession == true and concession_exceeds_customer_ceiling == true` | This customer has already had their full allowance |

### 5.4 Band 200–299 — escalation

| # | Rule | Condition | Why |
| --- | --- | --- | --- |
| 210 | `review-first-merchants-approve-everything` | `merchant_review_first == true and is_terminal_action == false` | The mode a merchant starts in: every action is drafted for a human. Stopping is excluded, so a case can always be closed |
| 220 | `large-actions-need-a-human` | `amount_minor >= 500000` | Any single action at or above ₹5,000.00, however confident the model is |
| 230 | `every-concession-is-reviewed-for-new-customers` | `is_concession == true and customer_tenure_days < 60` | Under two months, there is not yet enough history to tell a genuine payment problem from abuse |
| 231 | `repeat-concessions-are-reviewed` | `is_concession == true and prior_concession_count >= 2` | A third concession is a commercial decision about the relationship, not a recovery tactic |
| 240 | `writing-off-money-is-a-human-decision` | `action_type == 'stop_and_write_off'` | Abandoning a receivable is never taken unattended |
| 250 | `low-confidence-cases-are-reviewed` | `is_money_movement == true and recovery_likelihood < 150` | When the scheduler itself puts recovery below 15%, a person decides whether the attempt is worth spending |

### 5.5 Band 300–399 — ceilings

| # | Rule | Condition | Ceiling |
| --- | --- | --- | --- |
| 310 | `concession-absolute-ceiling` | `is_concession == true` | `cap_amount_minor = 200000` (₹2,000.00) |
| 311 | `concession-proportionate-to-the-subscription` | `is_concession == true` | `cap_percent = 25` of `subscription_mrr_minor` — conceding more than a quarter of what the customer pays each month destroys the value it was meant to protect |

Both match every concession; the tighter binds. On a ₹1,000/month subscription the proportionate cap
(₹250.00) wins; on a ₹100,000/month subscription the absolute cap (₹2,000.00) wins.

### 5.6 Band 900+ — the permits

| # | Rule | Condition | Covers |
| --- | --- | --- | --- |
| 910 | `permit-outreach` | `is_outreach == true and consent_state == 'granted'` | Contacting a consenting customer about a payment that failed |
| 920 | `permit-authorised-debit-retries` | `is_debit_retry == true and authorisation_decision == 'authorised'` | The core recovery action |
| 930 | `permit-instrument-and-mandate-repair` | `action_type in ['request_instrument_update', 'request_mandate_reauth', 'trigger_step_up', 'send_payment_link']` | Asking the customer to fix the underlying problem — the only recovery path for the terminal failure classes |
| 940 | `permit-bounded-concessions` | `is_concession == true` | Conceding within the budget and the ceilings above. This is what "bounded authority" means in practice |
| 950 | `permit-stopping` | `is_terminal_action == true` | Anvil may always stop, escalate or close a case. Choosing to do nothing further is never blocked by policy |

---

## 6. Worked examples

All from the shipped default bundle. Run `python scripts/tour.py` (section 06) or
`pytest tests/unit/test_policy.py` to reproduce.

### 6.1 Denials

| Facts | Effect | Rule | Reason |
| --- | --- | --- | --- |
| Reminder, consent granted, 23:00 IST | `DENY` | `quiet-hours` | *No outreach between 21:00 and 8:00 IST. A step-up authentication challenge is exempt because the customer is waiting on it in real time; nothing else is.* |
| Reminder, consent withdrawn, 11:00 IST | `DENY` | `consent-withdrawn-blocks-outreach` | *The data principal has withdrawn or let lapse their consent for this purpose…* |
| Reminder, consent granted, one contact already in 24h | `DENY` | `contact-frequency-24h` | *At most 1 contact in any rolling 24 hours…* |
| Reminder, last contact 6 hours ago | `DENY` | `minimum-gap-between-contacts` | *At least 20 hours between two contacts, whatever the rolling window counts allow.* |
| `retry_debit`, `risk_declined`, authorised | `DENY` | `never-retry-a-risk-decline` | *Retrying a risk decline is worse than doing nothing…* |
| `retry_debit`, `insufficient_funds`, authorisation denied | `DENY` | `unauthorised-actions-never-execute` | *No money moves without a valid authorisation…* |
| `retry_debit`, authorised, 4 attempts already in the cycle | `DENY` | `mandate-cycle-attempt-cap` | *No more than 4 debit attempts against one mandate cycle.* |
| ₹500 win-back with ₹200 of budget headroom | `DENY` | `concession-must-not-exceed-the-budget` | *The merchant's authorised concession budget has no room for this…* |

Note what is *not* denied: the same reminder at 23:00 IST with `purpose = step_up_authentication` is
allowed, because the customer is waiting on it.

### 6.2 Escalation

A ₹6,000 win-back to a 10-day-old customer with three prior concessions, on a review-first merchant,
matches four approval rules at once:

```
effect            require_approval
matched rule      repeat-concessions-are-reviewed        (the last approval rule in priority order)
approval_reasons  review-first-merchants-approve-everything
                  large-actions-need-a-human
                  every-concession-is-reviewed-for-new-customers
                  repeat-concessions-are-reviewed
capped to         ₹2,000.00 by concession-absolute-ceiling
```

The operator sees every reason, not just the first, and the cap is applied to the amount they are asked to
approve.

### 6.3 Capping

A ₹4,000.00 win-back discount on a ₹1,000.00/month subscription, consent granted, tenure 800 days:

```
effect       allow
capped       ₹250.00   (25% of MRR is tighter than the ₹2,000.00 absolute ceiling)
reason       Contacting a consenting customer about a payment that failed is permitted.
             Capped from ₹4,000.00 to ₹250.00 by concession-proportionate-to-the-subscription
```

### 6.4 The two structural denials

```python
evaluate(bundle(rule("never-fires", 100, PolicyEffect.ALLOW, {"op": "never"})), facts())
# DENY: "no policy rule matched this action, and Anvil denies what no rule permits.
#        Add a rule covering it if it should be allowed."

evaluate(bundle(), facts())
# DENY: "the active policy bundle contains no rules, so nothing is permitted.
#        An empty policy denies everything by design; it is not a licence to act."
```

---

## 7. Mandate authorisation

`anvil/mandates/authorise.py` is the one place in Anvil allowed to return `AUTHORISED`, and it does so from
a single statement at the bottom of one function, after every constraint has been evaluated. There is no
early return that permits, no `except` that permits, and no default that permits. If a future check is added
and its branch is forgotten, the result is a denial, not a debit.

The check is **structural, not statistical**: every branch compares a number on the request against a number
on a stored authorisation row. A model can propose an action; it cannot influence the answer here, and it
never sees this code path.

### 7.1 Inputs and outputs

`authorise(request, auth, usage, now) -> AuthorisationOutcome`. Pure — it reads three objects and returns a
verdict, touching no database and mutating nothing. The caller records consumption separately and only if
the action actually executes, so a check that is never acted on costs the mandate nothing.

`AuthorisationRequest` carries only what an authorisation can be tested against:

| Field | Meaning |
| --- | --- |
| `merchant_id`, `customer_id`, `subscription_id` | counterparty |
| `action_type`, `amount` | what is proposed; the amount must be positive |
| `acting_agent` | the named delegate, when delegated authority is being exercised |
| `issuer_demands_afa` | set from a prior decline classified `auth_required`. The rail, not Anvil, is demanding a factor |
| `is_retry` | whether this re-presents the cycle's existing charge or raises a new one. The safe default (`False`) is the one that gets refused |
| `principal_reachable` | whether the principal can be asked, right now, to extend a delegated cap. An unattended overnight retry has nobody at the other end |

`AuthorisationOutcome` carries `decision`, `effective_cap`, `explanation`, `authorisation_id`,
`denial_reason`, `step_up_trigger`, and `checks` — every comparison that was made, passed or failed, with
the numbers it was made against. A bare `DENIED` proves nothing; the trail is what turns a decision into
evidence.

### 7.2 The check order

| # | Check | On failure |
| --- | --- | --- |
| 0 | **Counterparty** — merchant, customer, subscription binding, delegate identity, currency, and that the usage row belongs to this authorisation | `DENIED` / `counterparty_mismatch`, effective cap ₹0.00 |
| 1 | **Status** is `ACTIVE` | `DENIED` / `status_not_active` |
| 2 | **Validity window** — `valid_from <= now <= valid_until` (open-ended when `valid_until` is null) | `DENIED` / `outside_validity_window` |
| 3 | **The principal's single-debit ceiling** `max_amount_minor` | `DENIED` / `amount_exceeds_mandate` |
| 4 | **The delegate's per-transaction cap** `agent_per_txn_cap_minor` | `REQUIRES_STEP_UP` / `delegation_cap_exceeded`, or `DENIED` / `amount_exceeds_delegation` when the principal is unreachable |
| 5 | **Period caps** — `period_cap_minor` (principal) and `agent_period_cap_minor` (delegate), against `amount_debited_minor` already spent in the cycle | principal: `DENIED` / `period_cap_exceeded`. Delegate: step-up, or `DENIED` / `period_cap_exceeded` when unreachable |
| 6 | **Frequency** — the instant falls inside the usage row's cycle, and a fixed-frequency mandate does not already carry a presentation unless this is a retry | `DENIED` / `frequency_violation` |
| 7 | **Attempt allowance** — `attempts_used < max_attempts_per_cycle` | `DENIED` / `attempts_exhausted` |
| 8 | **Reserve Pay block** — a block authorisation must have an undrawn remainder covering the amount | `DENIED` / `block_insufficient` |
| 9 | **Issuer AFA demand** | `REQUIRES_STEP_UP` / `issuer_demands_afa` |

The counterparty check runs first, ahead of the order in ARCHITECTURE.md §8, on purpose: an authorisation
belonging to another customer, bound to another subscription, held by another delegate, or denominated in
another currency is not a *tighter* authorisation — it is the wrong object, and every comparison that
follows presupposes it is the right one.

A pending step-up is **carried, not returned immediately**. Asking a customer to authenticate for an action
that a later constraint would refuse anyway is a wasted interruption and an eroded trust in the prompt. Any
denial after step 4 therefore wins over a pending step-up.

### 7.3 Two step-up triggers, because they reach different people

| Trigger | Who is asked | For what |
| --- | --- | --- |
| `delegation_cap_exceeded` | the **principal** | extend the authority they delegated |
| `issuer_demands_afa` | the **customer** | re-authenticate on the rail |

Conflating them would send the wrong message to the wrong person. `"no"` and `"not yet"` are commercially
very different answers: an action inside the principal's own mandate but outside the cap delegated to an
agent is not an abuse — it is the exact situation UPI Circle exists to handle, and the right response is to
ask, not to abandon the money.

### 7.4 The effective cap

`effective_cap` is the tightest single-debit ceiling in force after intersecting every limit:

```
cap = max_amount
    ∩ agent_per_txn_cap
    ∩ (period_cap        − amount_debited_minor)
    ∩ (agent_period_cap  − amount_debited_minor)
    ∩ remaining_block
floored at zero
```

A Reserve Pay authorisation with no blocked amount yields zero, never "unlimited". The cap is populated on
**denials too**, because the most useful thing to tell a planner that asked for too much is how much it
could have asked for — that is what turns a refused debit into a `split_debit`.

### 7.5 Cycle accounting

`anvil/mandates/cycles.py`. An authorisation's limits are per *cycle*, so the cycle containing an instant has
to be derived the same way by every caller.

- Cycles are anchored on **`valid_from`**, not the calendar. A mandate registered on the 17th bills on the
  17th and its attempt allowance resets on the 17th; pretending it resets on the 1st would hand the customer
  a second allowance in their first month.
- Where `period_days` is declared, the cycle is exactly that long, so the period cap and the attempt
  allowance are measured over the same window. A cap measured over a window that is not the accounting
  window is not a cap.
- Otherwise the cycle comes from the declared frequency:

| Frequency | Cycle |
| --- | --- |
| `daily` | 1 day |
| `weekly` | 7 days |
| `fortnightly`, `biweekly` | 14 days |
| `monthly`, `as_presented` | 1 calendar month |
| `bimonthly` | 2 months |
| `quarterly` | 3 months |
| `half_yearly`, `semi_annual` | 6 months |
| `yearly`, `annual` | 12 months |
| anything unrecognised | 1 calendar month, and treated as **fixed**, never as "as presented" |

Calendar months, not 30-day blocks: `add_months` clamps the day to the target month's length and is always
applied to the original anchor, so 31 January plus one month plus one month is 31 March, not 28 March.
Iterating would let a due date walk backwards a day or two every leap year.

`CycleWindow` is half-open `[start, end)` so consecutive cycles tile the timeline without an instant
belonging to two of them — which would let one debit be accounted against either of two allowances. The
`index` is 0 for the cycle beginning at `valid_from` and negative before it, which keeps the function total;
the validity-window check, not this one, refuses a debit raised too early.

`is_as_presented()` accepts only the literal `as_presented`. Anything unrecognised is held to
one-presentation-per-cycle, because failing closed on an unknown frequency costs at most a delayed debit,
while failing open costs a duplicate one.

### 7.6 Retry allowance

Consumption lives on `authorisation_usages`, one row per `(authorisation_id, cycle_start)` under a unique
constraint, holding `attempts_used`, `amount_debited_minor` and `last_attempt_at`. A cycle rollover is a new
row, not a destructive reset, so the history of how much of a mandate was used, and when, survives.

Two independent ceilings govern retries and the tighter binds:

| Ceiling | Default | Enforced by |
| --- | --- | --- |
| `Authorisation.max_attempts_per_cycle` | 3 | the authorisation check (`attempts_exhausted`) |
| `MAX_ATTEMPTS_PER_CYCLE` | 4 | the policy rule `mandate-cycle-attempt-cap` |

With the shipped defaults the mandate's own allowance binds first.

### 7.7 Worked authorisations

A ₹1,499.00 retry against a ₹2,000.00 monthly UPI Autopay mandate anchored 17 January, checked on
3 September 2026:

```
AUTHORISED  Authorised ₹1,499.00 against upi_autopay authorisation aut_1: attempt 1 of 3 in the
            cycle beginning 2026-08-17 00:00 UTC, effective ceiling ₹2,000.00.
```

| Scenario | Decision | Explanation |
| --- | --- | --- |
| Amount ₹2,500.00 | `DENIED` `amount_exceeds_mandate` | *₹2,500.00 exceeds the mandate's single-debit ceiling of ₹2,000.00.* |
| Three attempts already spent | `DENIED` `attempts_exhausted` | *3 of 3 permitted attempts have already been spent in this cycle.* |
| New presentation on a monthly mandate that already carries one | `DENIED` `frequency_violation` | *a monthly mandate permits one presentation per cycle and this cycle already carries 1.* |
| Delegated cap ₹500.00, principal reachable | `REQUIRES_STEP_UP` `delegation_cap_exceeded` | *₹1,499.00 is inside the principal's ₹2,000.00 mandate but above the ₹500.00 per-transaction cap delegated to agent anvil-agent.* Effective cap ₹500.00 |
| Same, unattended (`principal_reachable=False`) | `DENIED` `amount_exceeds_delegation` | *…and the principal cannot be asked to extend it for an unattended action.* |
| Request acting as the merchant against a delegated mandate | `DENIED` `counterparty_mismatch` | *authority is delegated to agent anvil-agent; the request is acting as the merchant directly.* Effective cap ₹0.00 |
| ₹2,000.00 cap over 30 days, ₹1,000.00 already debited | `DENIED` `period_cap_exceeded` | *₹1,499.00 on top of ₹1,000.00 already debited would breach the ₹2,000.00 cap over 30 days.* Effective cap ₹1,000.00 |
| Reserve Pay block with ₹200.00 undrawn | `DENIED` `block_insufficient` | *₹1,499.00 exceeds the ₹200.00 still undrawn on the blocked amount.* |
| Issuer demands AFA | `REQUIRES_STEP_UP` `issuer_demands_afa` | *the issuer requires an additional factor of authentication for this debit.* |
| Revoked mandate | `DENIED` `status_not_active` | *authorisation status is revoked, not active.* |
| No authorisation found at all | `DENIED` `no_authorisation` | *No stored authorisation covers ₹1,499.00 for customer cus_1.* |

`denied_without_authorisation()` is kept in this module so that **every** `DENIED` in the system is
constructed here, and the registry cannot accidentally invent a friendlier answer when it finds no rows.

### 7.8 What is and is not wired up

`anvil/mandates/` ships the authorisation check and the cycle arithmetic. The registry that selects which
authorisation to test, the consumption write-back, and step-up persistence are not built; the graph reaches
them through `AuthorisationPort` (`anvil/graph/ports.py`), which the simulator and the API demo satisfy. The
`step_up` node's interrupt is real — LangGraph commits the checkpoint before the node yields.

---

## 8. Consent

`anvil/channels/consent.py`. The DPDPA 2023 does not recognise general consent. A data principal consents to
a *purpose*, having been shown a *notice*, and may withdraw at any time with the same ease as granting.
Anvil models that literally: `ConsentReceipt` is keyed by `(customer, purpose, notice_version)` and the gate
looks up exactly the purpose the send is about to serve. A grant for `payment_failure_notice` authorises
nothing about `promotional_winback`.

Three commitments, each ruling out an easier implementation:

1. **Fail closed.** `ConsentGate.require()` raises `ConsentMissing` on anything that is not an affirmative,
   currently-effective grant. "No receipt found" and "receipt withdrawn" are the same answer at the send
   boundary; they differ only in what gets written down.
2. **Withdrawal never mutates.** Withdrawing writes a *new* receipt in the `WITHDRAWN` state. The question a
   regulator asks is not "is this person opted in today" — it is "were you allowed to send the message you
   sent on the 14th", and only an append-only history can answer that.
3. **Withdrawal triggers erasure.** Section 6(6) obliges the fiduciary to cease processing and erase. The
   gate publishes a purpose-scoped event on topic `dpdpa.erasure_requested` to the transactional outbox in
   the same transaction as the withdrawal receipt, so a crash between the two is not possible. Ledger and
   audit rows are tombstoned rather than deleted.

### 8.1 Resolution

`resolve_consent(receipts, customer_id, purpose, at)` reduces a receipt history to one decision. Pure, total,
deterministic; receipts for other purposes are ignored rather than rejected, so a caller may hand over a
customer's whole history without pre-filtering. The instant must be timezone-aware.

1. A withdrawal already in effect at `at` invalidates every grant made **before** it. Re-granting afterwards
   is a new, valid grant — that is what a preference centre does when someone opts back in.
2. Among surviving grants, the most recent governs. Ties break on receipt id, so two receipts stamped at the
   same microsecond resolve identically on every machine.
3. With no surviving grant, the state reported is the most informative true statement available.

| History at evaluation time | State | Reason |
| --- | --- | --- |
| No receipts for this purpose | `never_granted` | `no consent receipt for purpose payment_recovery_outreach` |
| Granted 40 days ago under notice `v3` | `granted` | `granted under notice v3` |
| Granted, then withdrawn 10 days ago | `withdrawn` | `consent withdrawn at 2026-08-24T10:00:00+00:00` |
| Granted, withdrawn, re-granted 2 days ago under `v4` | `granted` | `granted under notice v4` |
| Granted, with an `expires_at` now past | `expired` | `consent expired at 2026-08-04T10:00:00+00:00` |
| Granted for a different purpose only | `never_granted` | the grant is invisible to this purpose |

A grant is effective at an instant only when its state is `GRANTED`, `granted_at <= at`, no `withdrawn_at`
has passed, and no `expires_at` has passed (`ConsentReceipt.is_effective_at`).

### 8.2 The gate

`ConsentGate` is constructed per transaction, holding a repository and an outbox publisher bound to the
caller's session, so there is never a question about which transaction a write landed in.

| Method | Behaviour |
| --- | --- |
| `effective_consent(...)` | returns a `ConsentDecision`. **Never raises for a negative answer** — the dispatcher needs the decision as a value so it can persist the suppression before anything else happens |
| `require(...)` | asserts effective consent or raises `ConsentMissing`. For callers with no suppression-recording path of their own |
| `grant(...)` | writes a `GRANTED` receipt. Requires a non-blank `notice_version`; refuses an `expires_at` at or before the grant. `notice_summary` and `evidence_reference` are what make the receipt worth having: consent without a record of what the person was shown is an assertion, not evidence |
| `withdraw(...)` | writes a new `WITHDRAWN` receipt (notice version `withdrawal`) and publishes the erasure event, partitioned by customer. Built without a publisher, it logs `consent.withdrawn_without_outbox` rather than silently skipping |

The erasure payload is `{customer_id, merchant_id, purpose, consent_receipt_id, requested_at, reason,
scope: "purpose"}`. Purpose-scoped on purpose: withdrawing marketing consent does not oblige us to forget
that an invoice went unpaid, and over-erasing would break the financial record the same Act expects us to
keep.

The resolved state reaches the policy engine as the `consent_state` fact, evaluated for exactly the purpose
the action serves. The graph's default is `never_granted`.

---

## 9. Contact frequency and quiet hours

`anvil/channels/frequency.py`. Stopping rules, so everything here is arithmetic over the append-only
`ContactLedger`, evaluated by a pure function with no model, no I/O and no discretion.

The module exists to keep three questions apart that are usually collapsed into one:

- **Does this message count?** Always. Every contact that goes out is written to the ledger, whatever its
  purpose. A step-up challenge at 14:00 is real intrusion on the customer's attention and must make the
  15:00 dunning message wait. **Exemptions are exemptions from being blocked, never from being counted** —
  that asymmetry is the whole trick.
- **Is it capped?** Promotional caps apply to promotional purposes only. `promotional_winback` is the single
  promotional purpose; the rest are service messages about a payment the customer already agreed to make.
  `step_up_authentication` is exempt from the overall caps as well: it is the second half of an action the
  customer initiated, and there is no useful sense in which we can decline to finish it.
- **Is now an acceptable hour?** Quiet hours apply to everything, including step-up. The single exemption is
  a message that is both step-up *and* flagged `time_critical` by its caller. A step-up challenge at 23:05
  because the customer just tapped "pay now" is not an intrusion; one at 23:05 because the agent decided to
  start a recovery is. Only the caller knows which it has — and `time_critical` is refused at construction
  on a promotional message.

### 9.1 `FrequencyPolicy`

| Field | Default | Applies to |
| --- | --- | --- |
| `max_contacts_24h` | 2 | every purpose except `step_up_authentication` |
| `max_contacts_7d` | 5 | every purpose except `step_up_authentication` |
| `min_gap_minutes` | 240 | every purpose except `step_up_authentication` |
| `max_promotional_24h` | 1 | `promotional_winback` |
| `max_promotional_7d` | 2 | `promotional_winback` |
| `promotional_min_gap_minutes` | 1440 | `promotional_winback` |
| `quiet_hours_start_ist` | 21 | everything, unless time-critical step-up |
| `quiet_hours_end_ist` | 8 | as above |

`start` is inclusive, `end` is exclusive, and the window may wrap midnight — the naive `start <= hour < end`
comparison gets the common 21-to-8 case wrong, so `in_quiet_hours()` handles the wrap explicitly, in IST,
because quiet hours are a statement about when a person is asleep rather than about UTC. **Equal bounds mean
no quiet window at all**, not a 24-hour one: a merchant that wants to send at any hour sets both to the same
value, and reading that as "silence all day" would take a permissive configuration and make it maximally
restrictive. `FrequencyPolicy.for_merchant()` builds one from the merchant row's quiet-hour columns plus cap
overrides.

### 9.2 The decision

`evaluate_frequency(message, contacts, policy, now)` is pure and total. It requires a timezone-aware `now`
and refuses a ledger containing another customer's rows. Contacts must cover at least the last seven days;
anything older is ignored, and anything in the future is treated as already having happened, which is the
conservative reading.

When several constraints are broken at once, the one **reported** is the one that clears last (ties broken
by precedence), and `earliest_allowed_at` is the maximum clearing time across *every* violation — a caller
that rescheduled to the reported reason's clearing time alone would come straight back into a different
suppression. A rolling-window cap clears when its oldest contact rolls out of the window; with an empty
window and a zero limit there is nothing to roll out, so the cap is treated as lasting a full window length.

| Scenario (now = 15:30 IST) | Allowed | Status | Reason / effect |
| --- | --- | --- | --- |
| No contacts at all | yes | — | `within all contact limits` |
| One contact 2 hours ago | no | `suppressed_frequency_cap` | `minimum gap between contacts is 240 minutes; only 120 have passed`; clears at 12:00 UTC |
| Two contacts in 24h | no | `suppressed_frequency_cap` | `contacts in 24h is at its cap of 2 (currently 2)` |
| Five contacts in 7d | no | `suppressed_frequency_cap` | `contacts in 7d is at its cap of 5 (currently 5)` |
| Win-back, one promo already today | no | `suppressed_frequency_cap` | `promotional messages in 24h is at its cap of 1 (currently 1)` |
| Any service message at 00:00 IST | no | `suppressed_quiet_hours` | `inside IST quiet hours 21:00-08:00; local time was 00:00 IST` |
| Step-up, **not** time-critical, at 00:00 IST | no | `suppressed_quiet_hours` | caps waived, quiet hours still apply |
| Step-up, time-critical, at 00:00 IST | yes | — | exemptions: overall caps waived; quiet hours waived |
| Step-up over the overall 24h cap | yes | — | exemption: *overall caps waived: step-up completes an action the customer initiated* |

The decision always reports the counts (`contacts_24h`, `contacts_7d`, `promotional_24h`,
`promotional_7d`, `minutes_since_last_contact`) so a suppression row records the arithmetic, not just the
verdict.

### 9.3 Two layers, deliberately different numbers

The policy bundle and the frequency evaluator both constrain contact. They are separate settings, consulted
at different moments — the bundle when the action is evaluated, the evaluator at the channel boundary with
the ledger as it stands at send time — and the tighter of the two binds.

| Constraint | Default bundle | `FrequencyPolicy` |
| --- | --- | --- |
| Contacts per rolling 24h | denies at 1 | 2 |
| Contacts per rolling 7d | denies at 3 | 5 |
| Minimum gap | 20 hours | 240 minutes |
| Promotional per 24h / 7d | — | 1 / 2 |
| Promotional minimum gap | — | 1440 minutes |
| Quiet hours | 21:00–08:00 IST, `step_up_authentication` purpose exempt | 21:00–08:00 IST, step-up exempt only when `time_critical` |
| Contacts on one case | 4 | — |

### 9.4 The channel boundary

`OutboundMessage` is composed without permission to send it — composing and being allowed to send are
separate acts, which is what lets the system persist "we composed this and then declined to send it, for
this reason" as a first-class record. Adapters may not return a suppression status at all
(`SENDABLE_STATUSES`); suppression is the dispatcher's decision, taken before an adapter is consulted. An
adapter that returned one would be reporting a decision it is not entitled to make.

Note the state of the module: the consent gate, the frequency evaluator and the adapters exist and are
tested; the dispatcher that composes them into one send path is not yet built. The graph reaches outreach
through `ChannelPort.dispatch`, whose contract states that the implementation runs its own consent,
frequency and quiet-hours checks and may refuse.

---

## 10. Adding a policy rule

### 10.1 If the rule needs a fact that does not exist

Add the fact first. Four edits, in `anvil/policy/facts.py` unless noted:

1. A `_spec(...)` entry in `FACT_SPECS` declaring kind, description, and vocabulary or bounds.
2. The matching field on `PolicyFacts`, with the conservative default.
3. If it is a relation between other facts, add it to `DERIVED_FIELDS` and compute it in `_derive`.
4. Populate it in `_facts_for()` in `anvil/graph/nodes/gate.py` — and only from something Anvil observed
   itself.

The import-time drift check refuses to load the module if the catalogue and the model disagree, so a
half-finished fact fails immediately rather than becoming a rule that silently never matches.

### 10.2 Adding the rule

Rules reach a bundle two ways. Editing `anvil/policy/defaults.py` changes what every new merchant starts
with and is a code change. Compiling merchant prose produces a new bundle for one merchant at runtime. Both
end in a new content hash and a human approval.

Worked example — refusing win-back discounts to very new customers:

```python
from anvil.domain.enums import PolicyEffect
from anvil.policy.compiler import ProposedRule, assemble
from anvil.policy.defaults import default_bundle
from anvil.policy.expressions import all_of, eq, lt

active = default_bundle()

carried = [                                  # the proposal is the whole bundle, floor included
    ProposedRule(
        name=r.name, priority=r.priority, effect=r.effect, conditions=r.conditions,
        description=r.description or "",
        cap_amount_minor=r.cap_amount_minor, cap_percent=r.cap_percent,
    )
    for r in active.rules
]

new_rule = ProposedRule(
    name="no-winback-to-brand-new-customers",
    priority=170,                            # a prohibition, so the 100-199 band
    effect=PolicyEffect.DENY,
    conditions=all_of(
        eq("action_type", "offer_winback_discount"),
        lt("customer_tenure_days", 30),
    ),
    description=(
        "A win-back discount to someone who has been a customer for under a month "
        "is a discount on acquisition, not a recovery."
    ),
)

result = assemble([*carried, new_rule], active=active,
                  prose="never win-back-discount a customer under 30 days old")
```

Real output:

```
version 2, 28 rules
old hash  7023ab4499ea6cce22a980b4dbf5e781d9e4f9136c181a5aafa6f75702b69083
new hash  002d660fbf8fbfa4052419619f57a2e2ef4c21f422aef0f59f4c715060eb4013
requires_review True

summary   1 rule(s) added: no-winback-to-brand-new-customers
render    + no-winback-to-brand-new-customers
              now: [170] DENY when (action_type == 'offer_winback_discount' and customer_tenure_days < 30)
```

A ₹200 win-back to a 12-day-old customer, which the active bundle merely escalated
(`every-concession-is-reviewed-for-new-customers`), is now denied by name.

Omit the carried floor and the compilation is refused with one problem per missing regulatory rule:

```
the compiled policy was refused for 5 reason(s)
 - 'consent-withdrawn-blocks-outreach' is a regulatory rule and cannot be removed. A merchant cannot
   consent away a customer's rights on their behalf
 - 'no-consent-no-contact' is a regulatory rule and cannot be removed. …
 - 'promotional-winback-needs-its-own-consent' …
 - 'quiet-hours' …
 - 'unauthorised-actions-never-execute' …
```

### 10.3 Checklist

- [ ] Every fact the condition tests exists in the catalogue, and the literal types match.
- [ ] The priority sits in the right band, and at or above 100.
- [ ] The description says what the rule does and why, in the words a merchant would use to approve it —
      it is hashed, and it becomes the `reason` on every decision the rule produces.
- [ ] A `CAP` rule declares `cap_amount_minor`, `cap_percent`, or both.
- [ ] The immutable floor is carried through unchanged.
- [ ] The diff reads correctly, and the content hash changed.
- [ ] A human activates the bundle. `PROPOSED` bundles are not loaded by the evaluator.

### 10.4 Tests to write

Follow `tests/unit/test_policy.py`:

- One fact set that fires the rule, asserting `decision.matched_rule_name`.
- One neighbouring fact set that does **not** fire it — the boundary case, since off-by-one on a `gte` is
  the failure mode that survives review.
- For a DENY, that it is reached before the permits, by asserting `decision.denied` on facts a permit would
  otherwise cover.
- For a CAP, the resolved ceiling, remembering that a percentage resolves against `subscription_mrr_minor`.
- If the bundle is the default, that the rule count and immutable count still match what the console shows.

---

## 11. Where the code is

| Path | Contents |
| --- | --- |
| [`anvil/policy/expressions.py`](../../anvil/policy/expressions.py) | The operator set, validation, evaluation, `describe`, builders |
| [`anvil/policy/facts.py`](../../anvil/policy/facts.py) | The fact catalogue, `PolicyFacts`, derivation, sentinels |
| [`anvil/policy/evaluator.py`](../../anvil/policy/evaluator.py) | `CompiledRule`, `CompiledBundle`, `evaluate`, `PolicyDecision`, `RuleTrace` |
| [`anvil/policy/defaults.py`](../../anvil/policy/defaults.py) | The 27-rule starting bundle and its tunables |
| [`anvil/policy/compiler.py`](../../anvil/policy/compiler.py) | Propose, verify, diff, assemble; the `PolicyCompilerModel` protocol |
| [`anvil/policy/hashing.py`](../../anvil/policy/hashing.py) | Canonical form and `bundle_hash` |
| [`anvil/mandates/authorise.py`](../../anvil/mandates/authorise.py) | The single total authorisation check |
| [`anvil/mandates/cycles.py`](../../anvil/mandates/cycles.py) | Billing-cycle arithmetic |
| [`anvil/channels/consent.py`](../../anvil/channels/consent.py) | Consent resolution, the gate, erasure publication |
| [`anvil/channels/frequency.py`](../../anvil/channels/frequency.py) | Caps, minimum gaps, IST quiet hours |
| [`anvil/channels/base.py`](../../anvil/channels/base.py) | `OutboundMessage`, `DeliveryResult`, the adapter contract |
| [`anvil/db/models/policy.py`](../../anvil/db/models/policy.py) | `PolicyBundle`, `PolicyRule`, `PolicyEvaluation`, `Approval` |
| [`anvil/db/models/authorisation.py`](../../anvil/db/models/authorisation.py) | `Authorisation`, `AuthorisationUsage`, `StepUpChallenge` |
| [`anvil/db/models/comms.py`](../../anvil/db/models/comms.py) | `ConsentReceipt`, `OutreachMessage`, `ContactLedger`, `ErasureRequest` |
| [`anvil/graph/nodes/gate.py`](../../anvil/graph/nodes/gate.py) | The four gates in order, and `_facts_for` |
| [`tests/unit/test_policy.py`](../../tests/unit/test_policy.py) | The semantics, the default bundle, hashing, and the compiler |
