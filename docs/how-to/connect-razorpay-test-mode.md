# How to connect Razorpay test mode

Anvil runs entirely offline by default. Live mode is opt-in and additive: the
simulator keeps working alongside it.

## 1. Get test-mode credentials

From the Razorpay dashboard in **Test Mode**:

- **Settings → API Keys → Generate Test Key** gives a key id (`rzp_test_…`) and
  a secret. The secret is shown once.
- **Settings → Webhooks → Add New Webhook** gives a webhook secret. You choose
  this value; it is what signs the payloads.

## 2. Put them in `.env`

```bash
cp .env.example .env
```

```ini
ANVIL_MODE=live
ANVIL_RAZORPAY_KEY_ID=rzp_test_xxxxxxxxxxxxx
ANVIL_RAZORPAY_KEY_SECRET=xxxxxxxxxxxxxxxxxxxxxxxx
ANVIL_RAZORPAY_WEBHOOK_SECRET=xxxxxxxxxxxxxxxx
ANVIL_ANTHROPIC_API_KEY=sk-ant-xxxxxxxx
```

`.env` is gitignored. `ANVIL_MODE=live` **fails fast at startup** if any of the
four is missing, rather than discovering it on the first request.

## 3. Point the webhook at your machine

Razorpay needs a reachable URL. In development:

```bash
ngrok http 8000
```

Set the webhook URL to `https://<your-ngrok-host>/webhooks/razorpay` and
subscribe to at least `payment.failed`, `payment.captured`,
`subscription.charged`, `subscription.pending` and `subscription.halted`.

## 4. Verify the signature path

The most common integration bug is verifying against a re-serialised payload.
The signature is an HMAC-SHA256 over the **raw request bytes**; re-serialising
changes the byte sequence and guarantees a mismatch. Anvil reads the raw body for
exactly this reason.

Send a test webhook from the dashboard and confirm a `200`. Then change one
character of the secret and confirm a `400` — a verifier that never rejects
anything is not verifying.

## 5. What live mode changes

The gateway adapter swaps from the simulator to the real REST client. Both
satisfy the same Protocol, so nothing else changes. Model calls hit Anthropic
instead of the recorded fixtures.

What does **not** change: the ledger, the policy engine, the mandate registry and
the graph are identical. Live mode is an adapter swap, not a different code path.

## Going back offline

Unset `ANVIL_MODE` or set it to `offline`. No credentials are needed and nothing
touches the network.

---

## Razorpay's MCP server (optional, for development)

Razorpay hosts an MCP server exposing around fifty tools. Anvil configures it as
a **development instrument** — for exploring the API and recording fixtures — and
**not** as the product's payment client. See
[ADR-0014](../adr/0014-razorpay-mcp-as-a-development-tool.md) for why.

`.mcp.json` is committed and holds **no secret**: it interpolates
`${RAZORPAY_MCP_TOKEN}` from the environment.

```bash
export RAZORPAY_MCP_TOKEN=$(make mcp-token)
make mcp-check      # expect HTTP 200
```

`make mcp-token` derives `base64(key_id:key_secret)` from `.env` through Anvil's
settings, so the secret never appears on a command line where shell history would
keep it. It **refuses any key that is not `rzp_test_`**.

Restart your editor after exporting the variable, so the MCP client picks it up.

### What it does and does not cover

Useful for Anvil: `create_registration_link` (the mandate authorisation flow),
`fetch_tokens`, `revoke_token`, `create_order`, `fetch_payment`,
`create_payment_link`, `send_payment_link`, `create_refund`.

**Not available:** subscriptions and plans. Razorpay's tools reference lists no
tools for either, so creating a plan, creating a subscription or charging a
recurring invoice still goes through `anvil/gateway/` against the REST API.
