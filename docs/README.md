# Anvil documentation

Anvil is a revenue-recovery control plane for failed recurring payments. The
[root README](../README.md) states the problem, the thesis, and the one-command tour. Everything below
goes a level deeper.

Read in this order if you are new:

| Document | What it answers |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | How the system is shaped, where AI is used and deliberately is not, and the invariants that hold it together |
| [OPERATIONS.md](OPERATIONS.md) | How to install, configure, migrate, run, and troubleshoot it |
| [API.md](API.md) | Every HTTP endpoint, its parameters, responses, and errors |
| [DATA_MODEL.md](DATA_MODEL.md) | Every table, column, constraint, and the migration workflow |
| [LEDGER.md](LEDGER.md) | The money type, the chart of accounts, and the postings that move rupees |
| [POLICY.md](POLICY.md) | The policy expression language, the fact namespace, mandate authorisation, and consent caps |
| [AGENT.md](AGENT.md) | The recovery graph, its nodes, what reaches the model, and what happens when the model does not answer |
| [EVIDENCE.md](EVIDENCE.md) | The batch experiment, the simulator behind it, and what the numbers do and do not prove |
| [TESTING.md](TESTING.md) | The test pyramid, the markers, the fixtures, and how to add a test |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | Setup, the lint and typecheck loop, conventions, and the pre-PR checklist |
| [DESIGN.md](DESIGN.md) | The console's design system |

## The one-paragraph version

A debit fails. Intake classifies the decline against a taxonomy; the model is asked only where rules
genuinely fail. A scheduler proposes when and whether to retry, bounded by the mandate's remaining
authorisation. A policy engine evaluates the proposal against content-hashed rules and either permits
it, refuses it with a reason, or routes it to a named human. Only then does the ledger post — in
balanced double-entry legs, append-only, on an account chart that cannot be sidestepped. The model
decides. The ledger disposes. Nothing the model says can move money.

## Reading paths

- **Judging the submission**: root README, then [ARCHITECTURE.md](ARCHITECTURE.md), then
  [EVIDENCE.md](EVIDENCE.md) — including its threats-to-validity section.
- **Running it locally**: [OPERATIONS.md](OPERATIONS.md), then [API.md](API.md).
- **Changing the money paths**: [LEDGER.md](LEDGER.md) and [POLICY.md](POLICY.md), then
  [TESTING.md](TESTING.md) for the invariant suite that must stay green.
- **Changing the agent**: [AGENT.md](AGENT.md), then [POLICY.md](POLICY.md) for the gate it must pass.
