## What this changes

<!-- One paragraph. What is different afterwards, and why it needed to be. -->

## Why this approach

<!-- What else you considered. If this changes a module boundary, an invariant or
     a dependency direction, add an ADR in docs/adr/ as part of this PR. -->

## Checks

- [ ] `make lint` passes
- [ ] `make test` passes
- [ ] New behaviour has tests; anything enforcing a numbered invariant is marked
      `@pytest.mark.invariant`
- [ ] Docstrings explain *why*, not what
- [ ] No floats in the money path, no `datetime.now()`, no naive datetimes
- [ ] If this adds an LLM call, the docstring says why a deterministic
      implementation would be worse

## Anything you are unsure about

<!-- Genuinely useful. Say so here rather than hoping nobody notices. -->
