# Configure Claude for live classification

By default, Anvil runs in offline mode using a seeded deterministic fallback. To enable Anvil's intelligent classification and planning, you need to configure it with an Anthropic API key.

## 1. Set the API key

Create or edit the `.env` file at the root of the repository (or set the environment variable directly):

```bash
ANVIL_ANTHROPIC_API_KEY="sk-ant-api03-..."
```

Optionally, explicitly set the mode to live to enable live integrations:

```bash
ANVIL_MODE=live
```

!!! warning "Security and cost"
    The Anthropic API key is the only credential in the system that costs real money. Always set a spend limit in the Anthropic console before deploying.

## 2. Using Claude in the Journey stream

When running the API server locally, you can force the journey stream to use Claude instead of the deterministic fallback. In the console, the stream requests check the environment by default, but you can override this behavior by adding the `use_claude=true` query parameter to the stream endpoint:

```
GET /api/journey/stream?scenario=fast-retry&use_claude=true
```

## 3. Deterministic fallback (Degradation)

What happens when Claude is unavailable, rate-limited, or timing out? 

Anvil is designed to survive the model being gone. If the LLM call fails, the graph falls back to the deterministic pipeline:
1. `classify` maps the gateway code using static tables.
2. The fallback planner proposes conservative, safe actions (like scheduling a retry without offering any financial concessions).
3. The case still settles. 

This degradation path is not just theoretical; you can watch it run in the "degraded" journey scenario.

## 4. Cost monitoring

Every call to the LLM tracks token usage and translates it into a cumulative cost in paise (minor units of INR). This metric is tracked natively within Anvil's telemetry and batch reports.

You can view the total LLM spend in the console's Batch experiment screen, comparing the cost of the agent against the revenue it recovered.

## 5. Security: PII Redaction

Before any prompt is sent to Claude, it passes through `anvil.llm.redaction`. 

This step deterministically strips personally identifiable information (PII) from bank narrations, names, and contact details. The language model never sees raw customer data, and the redacted fields are re-injected or mapped back only after the response is parsed.

## 6. Model choice rationale

Anvil uses **Claude 3.5 Sonnet** (`claude-3-5-sonnet-20240620` or similar) for both classification and planning.

Why Sonnet? 
- **Speed:** It returns structured JSON classifications and multi-step plans in under two seconds.
- **Cost:** It is significantly cheaper than Opus while maintaining the reasoning capability needed for complex failure diagnosis.
- **Instruction following:** It strictly adheres to the closed action-space schemas defined by Anvil's system prompts.
