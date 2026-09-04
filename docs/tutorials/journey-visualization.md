# Watch a recovery unfold

By the end of this tutorial, you will have watched the Anvil recovery graph execute in real time, stepped through the decisions it makes, and seen how it fails safely when a model misbehaves or a human is required.

**Time:** about ten minutes.  
**You need:** Python 3.12 or newer. No database, no API keys, no Docker.

---

## 1. Start the API server

If you haven't already, set up your local environment:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Then, start the API server using `uvicorn`:

```bash
.venv/bin/python -m uvicorn anvil.main_api:app --port 8000
```

Alternatively, if you want to run the full environment with Docker Compose, you can run `make console`.

## 2. Open the console and navigate to Journey

Open your browser to <http://localhost:8000> (or `8060` if using the Docker console setup). You will land on the default dashboard. 

Click on the **Journey** navigation item. 

The Journey screen streams a live execution of a case moving through the Anvil graph. This is not a recording—what you are watching is LangGraph's actual state transitions, stepping node by node against the deterministic rules and the language model.

## 3. Node types in the graph

As the graph runs, you will see different node kinds light up. Each serves a specific architectural purpose:

- **Blue (step):** Deterministic execution. These nodes map codes to failure classes, score risk, or execute ledger actions. No model is involved.
- **Purple (model):** Language model execution. These nodes propose actions (diagnosis, planning). 
- **Amber (gate):** Fail-closed safety gates. These nodes (authorisation, policy) structurally enforce limits on what the model proposes.
- **Red (pause):** Durable interrupts. The graph stops here and waits for human input, committing its state to disk.

!!! quote ""
    **The model decides. The ledger disposes. Nothing the model says can move money.**

## 4. Run the "payday" scenario

Select **"Insufficient funds, and the patience to wait for payday"** from the scenario dropdown. 

Watch the nodes as they execute:
1. `ingest`: The case opens and the receivable is recognised on the ledger.
2. `classify`: The failure is deterministically mapped to `insufficient_funds`.
3. `score`: Baseline recovery and churn metrics are computed.
4. `diagnose` & `plan`: The model observes the state and proposes a retry.
5. `authorise` & `policy`: The gates confirm this action is legal and within budget.
6. `schedule`: The optimiser takes over. **Notice what happens here.** A balance failure does not clear tomorrow. Instead of eagerly retrying, the scheduler dynamically holds out for a salary-credit day (typically the 1st or the end of the month). A naive retry loop would have burned its attempts and gotten nothing.

## 5. Run the "out-of-bounds" scenario

Select **"The model proposes something it must not"** and watch the graph.

In this scenario, the language model is intentionally instructed to propose wiring money to the customer—an action entirely outside the closed, safe action space.

Watch the gates:
- The model proposes the illegal action in the `plan` node.
- The `authorise` node catches it. It structurally rejects the illegal proposal before the executor ever sees it.
- This is recorded as a model-safety event.

This demonstrates why the safety gates are separated from the model's intelligence. You cannot prompt-engineer away a deterministic block.

## 6. Run the "human-approval" scenario

Select **"The graph stops and waits for a person"**.

Watch the graph pause:
- The case runs normally through `ingest`, `classify`, and `plan`.
- At the `approval` node, the graph completely stops. 
- You will see a `Paused` event indicating that a durable interrupt has occurred. 

This is not just a UI state. The LangGraph thread has written a checkpoint to durable storage and yielded execution. You could kill the Python process right now, restart it, and the case would still be sitting there, waiting for you to approve it. Once approved, it resumes execution precisely where it left off.

## Where to go next

- [Architecture](../explanation/architecture.md) — to understand the thesis behind the model/gate separation.
- [Journey streaming API](../reference/journey-api.md) — to see the events that power this visualization.
