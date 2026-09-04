# Journey streaming API

The Journey API provides a live, server-sent events (SSE) stream of a single recovery case moving through the Anvil graph. This powers the visual console and serves as the primary teaching tool for understanding the system's execution topology.

---

## GET /api/journey/scenarios

Returns the available scenarios, the graph topology, and the purpose of each node.

**Response**

| Field | Type | Notes |
| --- | --- | --- |
| `scenarios` | array&lt;Scenario&gt; | The 7 built-in scenarios. |
| `topology` | object | The edges of the LangGraph topology. |
| `nodes` | array&lt;NodeMeta&gt; | Metadata for every node in the graph. |

**NodeMeta**

| Field | Type | Notes |
| --- | --- | --- |
| `name` | string | The node's internal identifier (e.g., `classify`). |
| `purpose` | string | A one-sentence explanation of what this node does. |
| `kind` | string | `step` (deterministic), `model` (LLM), `gate` (fail-closed), or `pause` (interrupt). |

### The 7 Scenarios

| Key | Title | What it teaches |
| --- | --- | --- |
| `fast-retry` | A technical decline, recovered in hours | The cheapest recovery: the rail failed, not the customer. |
| `payday` | Insufficient funds, and patience | The scheduler waiting for payday instead of eager retrying. |
| `terminal` | A revoked mandate, refused | Structural refusal of unauthorised attempts. |
| `degraded` | The language model is down | The deterministic fallback path keeping recovery alive. |
| `out-of-bounds` | The model proposes something it must not | Safety gates blocking illegal LLM actions. |
| `human-approval` | The graph stops and waits for a person | A real durable interrupt on a committed checkpoint. |
| `unknown-outcome` | The gateway times out | Idempotent ledger parking for unknown outcomes. |

### Node Purpose Map

| Node | Kind | Purpose |
| --- | --- | --- |
| `ingest` | `step` | Open the case and recognise the receivable on the ledger |
| `classify` | `step` | Map the raw reason code to a failure class |
| `score` | `step` | Recovery likelihood, churn risk and priority |
| `diagnose` | `model` | Infer what is actually wrong: can they pay, do they intend to |
| `plan` | `model` | Choose actions from a closed set, under a live concession budget |
| `authorise` | `gate` | Is there a stored right to do this? Structural, and it fails closed |
| `step_up` | `pause` | Paused: the customer must re-authenticate before this can proceed |
| `policy` | `gate` | Is it permitted, and how much of it? Deterministic. No match denies |
| `approval` | `pause` | Paused: a person must decide before any money moves |
| `schedule` | `step` | Solve for the best hour — a dynamic program over the hazard curve |
| `execute` | `step` | Do the thing. Idempotency key attached, outcome recorded honestly |
| `observe` | `step` | What did that mean? Continue, re-plan, or stop |
| `close` | `step` | Terminal. Write off anything genuinely lost |

---

## GET /api/journey/stream

Streams the execution of a selected scenario using Server-Sent Events (SSE).

| Parameter | Type | Default | Constraint |
| --- | --- | --- | --- |
| `scenario` | string | `fast-retry` | Must be one of the 7 scenario keys. |

**Event Types**

The stream emits four distinct types of events.

### 1. `case` event

Sent exactly once at the start of the stream. Contains the initial context for the recovery case.

```json
{
  "scenario": "fast-retry",
  "title": "A technical decline, recovered in hours",
  "teaches": "The cheapest recovery...",
  "case_id": "cse_ABC123",
  "customer": "Rahul Sharma",
  "bank": "Harbour National",
  "mandate": "upi_autopay",
  "amount": "₹1,499.00",
  "raw_code": "U30",
  "narration": "issuer unavailable",
  "code_is_unmapped": false,
  "failed_at": "Wed 02 Sep 00:30 IST",
  "true_failure_class": "issuer_technical"
}
```

### 2. `node` event

Sent every time a node completes execution in the graph. Contains a snapshot of the updated graph state.

```json
{
  "node": "plan",
  "purpose": "Choose actions from a closed set, under a live concession budget",
  "summary": "Proposed retry_debit for ₹1,499.00",
  "kind": "model",
  "state": {
    "status": "planning",
    "failure_class": "issuer_technical",
    "classified_deterministically": true,
    "recovery_likelihood": 850,
    "churn_risk": 120,
    "attempts": 0,
    "contacts": 0,
    "recovered": "₹0.00",
    "at_risk": "₹1,499.00",
    "conceded": "₹0.00",
    "degraded": false,
    "degraded_reason": null,
    "safety_events": 0,
    "next_action_at": null
  },
  "actions": [
    {
      "type": "retry_debit",
      "status": "proposed",
      "amount": "₹1,499.00",
      "authorisation": null,
      "policy": null,
      "rationale": "Retrying technical decline."
    }
  ]
}
```

### 3. `paused` event

Sent when the graph hits a durable interrupt (e.g., `approval` or `step_up`). The server holds for a few seconds to simulate the pause before automatically injecting a resume command.

```json
{
  "waiting_on": ["approval"],
  "note": "The checkpoint is committed. The process could be killed here and this case would resume from exactly this point."
}
```

### 4. `done` event

Sent when the graph reaches the `__end__` state.

```json
{
  "status": "recovered",
  "closure_reason": "payment_settled",
  "state": {
    "status": "recovered",
    "recovered": "₹1,499.00",
    "at_risk": "₹0.00"
  }
}
```
