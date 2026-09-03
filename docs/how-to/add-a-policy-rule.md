# How to add or change a policy rule

## The quick way: edit the default bundle

`anvil/policy/defaults.py`. Rules live in priority bands, and the band is the
design:

| Band | For |
|---|---|
| 0–99 | Regulatory and consent floors. Marked `immutable=True`. |
| 100–199 | Hard prohibitions — futile or harmful, rather than illegal. |
| 200–299 | Escalation to a human. |
| 300–399 | Ceilings. |
| 900+ | The permits. |

```python
_rule(
    "no-outreach-during-festivals",
    145,
    PolicyEffect.DENY,
    _and(_eq("is_outreach", True), _in("local_day_of_month_ist", [1, 2])),
    "No recovery outreach on the first two days of the month, when the "
    "merchant runs its own billing communications and a third message would "
    "be the one that annoys.",
),
```

The description is not optional and is not decoration. It is the text an operator
reads when this rule blocks something, and the reason must survive being read by
someone who was not there.

## Facts you can test

Only what is in `anvil/policy/facts.py`. The model forbids unknown keys, so a
typo is an error at the boundary rather than a rule that silently never matches.
The full catalogue is in that file, and the console's **Policy engine** screen
renders every rule's condition in English.

## Semantics you must know

- The **first matching DENY wins outright** and stops evaluation.
- `REQUIRE_APPROVAL` is **sticky** — nothing downgrades it back to ALLOW.
- The **tightest CAP wins**, and caps accumulate.
- **No match denies.** If you add a new action type, you must also add a permit,
  or it will be refused. This is deliberate; see
  [ADR-0006](../adr/0006-policy-denies-on-no-match.md).

## Test it

Add a case to `tests/unit/test_policy.py` constructing the facts that should
trigger it, then check it interactively:

```bash
make console   # Policy engine → evaluator
```

The evaluator shows the full rule-by-rule trace, including the rules that did
*not* match — which is what you need when the rule you expected to fire did not.

## The merchant-facing way: compile from prose

`anvil/policy/compiler.py` turns plain English into a proposed bundle. The model
authors the policy; it never *is* the policy. Every proposal is verified before a
human can activate it, and verification refuses:

- unknown fact names
- rules claiming a reserved priority below 100
- caps with no ceiling
- rules with no description
- **any attempt to drop or modify an immutable rule** — checked by canonical
  content, so keeping the name while changing the behaviour fails

A compiled bundle is `PROPOSED` until a human activates it, and the evaluator
will not load one that has not been approved.
