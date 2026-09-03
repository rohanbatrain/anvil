# Anvil documentation

Organised by what you are trying to do, following
[Diátaxis](https://diataxis.fr/). The four kinds answer different questions, and
mixing them is the most common way documentation becomes unusable, so they are
kept apart.

| If you want to… | Go to | Which is |
|---|---|---|
| **learn** how the system works by using it | [Tutorials](#tutorials) | study + action |
| **do** a specific thing | [How-to guides](#how-to-guides) | work + action |
| **look up** a fact | [Reference](#reference) | work + knowledge |
| **understand** why it is built this way | [Explanation](#explanation) | study + knowledge |

**Assessing this as a submission?** Start with [`../REVIEWING.md`](../REVIEWING.md),
which routes you through the highest-signal parts in five, fifteen and thirty
minutes.

---

## Tutorials

Learning by doing. Start here if the system is new to you.

- [**Recover your first payment**](tutorials/first-recovery.md) — open the
  console, work a real paused case through the approval queue, and watch the
  graph resume and settle. Fifteen minutes, no credentials.

## How-to guides

Recipes for a specific goal. They assume you already know roughly what you are
doing.

- [Run the batch experiment](how-to/run-the-batch.md)
- [Operations](how-to/operations.md) — setup, configuration, running the
  services, and what to do when something breaks
- [Add a decline code](how-to/add-a-decline-code.md)
- [Add or change a policy rule](how-to/add-a-policy-rule.md)
- [Connect Razorpay test mode](how-to/connect-razorpay-test-mode.md)
- [Fit the retry curves to real outcomes](how-to/fit-the-retry-curves.md)

## Reference

Facts, described as plainly as possible and not tailored to any task.

**The system**

- [The ten invariants](reference/invariants.md) — each guarantee, what enforces
  it, and the test that proves it
- [Money and the ledger](reference/ledger.md) — the `Money` type and the
  append-only double-entry ledger, in full
- [Policy, authorisation and consent](reference/policy.md) — the three
  deterministic gates
- [The recovery agent](reference/the-agent.md) — the graph, its nodes, what the
  model is shown, and what happens when the model is not there
- [Data model](reference/data-model.md) — every table, column and constraint

**Interfaces**

- [HTTP API](reference/api.md) — twelve endpoints, or read the live OpenAPI at `/docs`
- [Configuration](reference/configuration.md) — every environment variable,
  generated from the settings model
- [Console design system](reference/design-system.md) — the binding visual spec

**Method**

- [Evidence methodology](reference/evidence-methodology.md) — what the batch
  claims, how it is built, and where it stops being evidence
- [Testing](reference/testing.md) — 203 tests, no database or network
- [Glossary](reference/glossary.md) — mandate, UMN, e-NACH, AFA, dunning, and
  the rest of the vocabulary

## Explanation

Background and reasoning. Read when you want to understand rather than act.

- [**Architecture**](explanation/architecture.md) — the thesis, the invariants,
  the decline taxonomy, the authorisation model, and an explicit account of where
  AI is *not* used
- [Why a dynamic program, not a model, decides retry timing](explanation/why-not-an-llm-for-retry-timing.md)
- [How recovery is measured](explanation/the-evidence-model.md) — three arms,
  bootstrap intervals, and why the agent currently loses
- [Compliance](explanation/compliance.md) — DPDPA, RBI, and the state of UAP

## Architecture decision records

- [**All 13 decisions**](adr/) — each with the alternatives that lost, and the
  ones that turned out to be wrong

---

## Elsewhere in the repository

| File | What it is |
|---|---|
| [`../README.md`](../README.md) | The front door: problem, thesis, results, limitations |
| [`../REVIEWING.md`](../REVIEWING.md) | A guided path for someone assessing this |
| [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | Setup, the non-negotiable rules, and style |
| [`../SECURITY.md`](../SECURITY.md) | What this codebase does with sensitive data |
| [`board.html`](board.html) | A visual build board — open it in a browser |
| [`../scripts/tour.py`](../scripts/tour.py) | The whole system in one terminal run |
