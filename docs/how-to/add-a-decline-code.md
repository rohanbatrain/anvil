# How to add a decline code

Banks invent new reason strings constantly. Adding one is a two-line change plus
a test.

## 1. Find the right namespace

`anvil/domain/taxonomy.py` holds four tables: `_UPI_CODES`,
`_NACH_RETURN_CODES`, `_CARD_ISO_CODES` and `_TEXTUAL_CODES`. Put the code where
the rail actually emits it — the namespace matters, because the same string can
mean different things on different rails. `05` is a revoked mandate on NACH and
a plain "do not honour" on cards.

## 2. Map it to an existing failure class

```python
_UPI_CODES: dict[str, FailureClass] = {
    ...
    "U91": FailureClass.ISSUER_TECHNICAL,
}
```

**Do not add a new `FailureClass`** unless the recovery *posture* is genuinely
different from all ten existing ones. The enum is closed on purpose: the model is
constrained to emit only these members, and every class needs a retry curve, a
churn base rate and a place in the policy bundle. A new class is a real change;
a new code is not.

## 3. Add a test

In `tests/unit/test_risk.py`:

```python
def test_recognised_codes_resolve_without_a_model() -> None:
    for raw, expected in [
        ...
        ("U91", FailureClass.ISSUER_TECHNICAL),
    ]:
```

## 4. Check it in the console

```bash
make console
```

Open **Classifier**, type the code, and confirm it resolves rather than
escalating.

## If it should not resolve deterministically

Some strings are genuinely ambiguous without more context. Leave them out. The
classifier will escalate them, which is the correct behaviour — guessing between
"the customer cancelled" and "the issuer declined this time" is what sends an
insulting dunning email to someone who already left.

## If the string is free text rather than a code

`anvil/risk/classifier.py` holds `_PHRASE_RULES`, which match on phrases rather
than exact tokens. `"A/c bal low"` resolves through these. Add there instead, and
note the corroboration rules: a single free-text phrase never resolves on its
own, but two independent fields that agree will.
