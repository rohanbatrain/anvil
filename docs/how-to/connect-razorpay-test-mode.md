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
