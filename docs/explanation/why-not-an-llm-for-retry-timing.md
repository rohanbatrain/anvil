# Why a dynamic program, not a model, decides retry timing

"When should I retry this failed debit?" sounds like the most agentic question in
the domain. It is the one place where handing the decision to a language model
would be most tempting and most wrong, and the reasoning is worth setting out
because the same reasoning applies every time someone proposes adding an LLM to
something.

## It is not the question it appears to be

The naive framing is *"is now a good time to retry?"* Framed that way it looks
like a judgement call, and judgement calls look like model work.

The actual question is *"given a finite number of retry attempts against this
mandate, when should I spend the next one?"* Mandates permit two to four
presentments per cycle. Every attempt spent is one unavailable later. That makes
it a **sequential decision problem**, and treating it greedily is precisely how
conventional dunning burns three attempts in the 48 hours after a failure and has
nothing left when the customer's salary lands.

## The problem has a closed-form structure

Let `A` be the amount at risk and `p(k,t)` the probability that the *k*-th
remaining attempt settles if made at hour `t`. Let `V(k,t)` be the expected value
of playing `k` attempts optimally from `t` onward:

```
V(0, t) = 0
V(k, t) = max over t' ≥ t of [ p(k,t')·A + (1 − p(k,t'))·V(k−1, t' + gap) ]
```

Evaluating that naively is quadratic in the horizon. It does not need to be: the
expression inside the max does not depend on `t`, so `V(k,·)` is a **suffix
maximum** of a function computed once per hour. One backward pass per attempt
level, giving `O(attempts × horizon)` — a few thousand exact `Decimal` operations,
fast enough to run inline on every case and simple enough to check by hand.

A problem with a closed-form structure and a cheap exact solution does not need a
model. It needs the solution.

## What the model would cost

**Accuracy.** Retry success is a well-posed estimation problem with abundant
labelled data. A hazard function fitted to outcomes beats a language model's
intuition about payday, and it is not close.

**Reproducibility.** The batch experiment only means something because the same
seed produces the same answer on every machine. A model call in the scheduler
would make the headline number unreproducible, and an unreproducible number is
not evidence.

**A number we need.** The argmax also yields the *value of the remaining
attempts*, which the planner compares against the cost of a concession. Offering
₹200 to save a subscription whose remaining retries are already worth ₹1,100 in
expectation is giving money away; offering it when they are worth ₹40 is good
business. Asking a model for a date returns a date. The dynamic program returns
the date **and** the number that makes the next decision possible.

**Latency and money.** A model call per scheduling decision, on every case, on
every re-plan.

## The honest weakness

The scheduler is only as good as its curves, and the curves are hand-written
priors. The calibration report says they are systematically over-confident by
about ten points, and in the current batch a naive fixed schedule outperforms the
optimiser.

That is a real weakness and it is **not an argument for using a model instead**.
It is an argument for fitting the curves, which is a well-understood statistical
exercise with a well-understood answer. `anvil/risk/calibration.py` is the
instrument that measures it and
[the how-to guide](../how-to/fit-the-retry-curves.md) is the procedure.

"Our arithmetic is miscalibrated and here is the measurement" is a fixable
position. "Our model said so" is not.

## The general rule

Before adding a model call, three questions:

1. **Is the input genuinely unstructured?** Free text from dozens of banks with
   no shared vocabulary — yes. A number and a timestamp — no.
2. **Is the output space open?** Prose in a customer's language — yes. An hour
   within a horizon — no.
3. **Would a deterministic implementation be reproducible and auditable?** If
   yes, the burden is on the model to be *better*, not merely adequate.

Retry timing fails all three. Failure diagnosis, recovery planning under a live
budget, customer communication and policy compilation pass them, which is why
those four are where the model is used.
