# Recover your first payment

By the end of this you will have taken a failed subscription debit, seen why the
agent proposed what it did, approved it as a human operator, and watched a
paused workflow resume and settle the money.

**Time:** about fifteen minutes.
**You need:** Python 3.12 or newer. No database, no API keys, no Docker.

---

## 1. Install and start

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
make console
```

Open <http://localhost:8000>. You should land on the **Approval inbox**.

If the page is empty of items, the system decided every proposed action
autonomously — reload, or read step 6 for why that can happen.

## 2. Read what you are being asked to approve

Take the first item. It has more on it than a typical approval queue, and each
part is there for a reason:

- **The action and amount.** What will happen if you approve.
- **Recovery likelihood and churn risk**, on 0–1000 scales. Computed
  deterministically from the failure class and the customer's history.
- **Why a human is being asked.** Which rule escalated this rather than letting
  it run unattended.
- **The agent's own reasoning**, verbatim. This matters: approving an action
  whose reasoning you cannot see is not meaningfully being in the loop.

Notice the reasoning probably begins *"Deterministic fallback:"*. The language
model is unavailable in this default configuration, and the system is still
working — that is the degradation path, and you are watching it run.

## 3. Approve it

Click **Approve**.

What happens next is the part worth understanding. That queue item was not a row
in a table waiting to be flagged. It was a **genuinely paused LangGraph thread**,
sitting on a checkpoint committed to durable storage, blocked inside an
`interrupt()` call.

Approving resumes that thread. It then runs, for real:

1. **Authorisation** — is there a valid mandate covering this amount?
2. **Policy** — does the merchant's active bundle permit it?
3. **Scheduling** — the dynamic program picks the hour.
4. **Execution** — the debit is presented to the issuer model.

The timeline that appears underneath is the graph's own history. You will see
`approval`, then `schedule` with the hour it chose and why, then `execute`.

## 4. Try to approve it again

You cannot. The item carried a version number, and resolving it incremented that.
A second attempt with the version you were shown returns **409 Conflict**.

This is not decoration. Two operators who open the same ₹40,000 concession must
not both be able to approve it, and the alternative — last write wins — is how
that happens.

## 5. See where the money went

Open the **Ledger** screen. Set an amount, a concession, and whether the debit
settles, then press **Post**.

Each transaction shows its entries and, underneath, `debits = credits` with a
**balances** badge. This is not a rendering of a stored result: the balance check
that runs here is the same one that runs before any real commit. If you can make
this screen say **UNBALANCED**, you have found a genuine defect.

Look at the concession posting in particular. It has *four* legs, because two
distinct things happen — a concession costs **revenue**, not cash, and it
consumes the earmarked budget that authorised it. Netting them into two legs
would make "how much of the authorised budget has been used?" unanswerable from
the ledger.

## 6. Find out why the agent is sometimes not asking you

Open the **Policy engine** screen and scroll the 27 rules.

Now use the evaluator at the bottom. Set the action to `retry_debit`, the
authorisation to `denied`, and press **Evaluate**. The result is `DENY`, matched
by `unauthorised-actions-never-execute` — one of five rules marked
**regulatory**, which the natural-language policy compiler is forbidden from
weakening or removing.

Now try something that no rule mentions at all. Set consent to `never_granted`
with an outreach action. It is denied — and the important part is *why*: **a
policy gap blocks an action rather than allowing it.** Forgetting to write a rule
is safe here. That is the inverse of how most rule engines behave, and
[ADR-0006](../adr/0006-policy-denies-on-no-match.md) explains the reasoning.

## 7. Watch the scheduler think

Open **Retry scheduler**. Leave the class as `insufficient_funds`, set the
failure date to the **18th** of a month, and solve.

It waits nearly two weeks. The explanation says why: it is holding out for a
salary-credit day, because a customer who just bounced a debit for want of funds
does not have funds tomorrow either.

Now change the class to `issuer_technical` and solve again. Six hours — the
minimum permitted gap. A rail failure means the customer could always pay and the
bank could not take the money, so it is the cheapest recovery there is.

Then try `instrument_expired`. **Refused.** The card will be just as expired
tomorrow, and every attempt spent on it is one not spent on a recoverable case.

No model was involved in any of those three answers.

---

## What you have seen

- A durable human-in-the-loop pause that survives process restarts
- Optimistic locking preventing a double approval
- A double-entry ledger that refuses to be unbalanced
- A policy engine that fails closed on a gap
- A retry optimiser that is arithmetic, not a prompt
- The system working with the language model switched off

## Where to go next

- [Run the batch experiment](../how-to/run-the-batch.md) to see all of this
  measured against a control arm — including the finding that a naive fixed
  schedule currently beats it
- [Architecture](../explanation/architecture.md) for the reasoning underneath
- [The ten invariants](../reference/invariants.md) for what is guaranteed and
  what enforces it
