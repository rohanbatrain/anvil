# How to deploy the public demonstration

Target: **https://anvil.rohanbatra.in**

## The security posture, first

Everything below follows from one decision, and it is worth understanding before
you run any of it.

**The public instance holds no payment credentials at all.** It runs in offline
mode against the seeded simulator. It cannot leak a Razorpay key because there is
none on the machine, and it cannot move money because nothing is wired to a
payment API. The only secret it holds is the console password, which protects the
demonstration and nothing else.

Live mode and webhooks stay **local, behind a temporary tunnel**, only while you
are testing. A permanently public endpoint holding real credentials is a
liability with no upside for a demonstration.

The deploy workflow enforces this: it fails the build if the deployed instance
reports anything other than `offline`.

| | Local `.env` | Deployed host |
|---|---|---|
| Razorpay key id / secret | yes, for testing | **never** |
| Razorpay webhook secret | yes, for testing | **never** |
| Anthropic API key | yes | **never** |
| `ANVIL_CONSOLE_PASSWORD` | not needed | **yes** — the only secret |
| `ANVIL_MODE` | `live` while testing | `offline` |

---

## Pre-flight

```bash
make lint          # ruff + mypy
make test          # 203 tests
make batch         # the experiment still runs
docker build -t anvil:local .
docker run --rm -p 8000:8000 -e ANVIL_CONSOLE_PASSWORD=test anvil:local
```

Then, against the container:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/          # 401
curl -s -o /dev/null -w "%{http_code}\n" -u reviewer:test http://localhost:8000/  # 200
curl -s http://localhost:8000/health                                     # 200, mode offline
```

If the first is not `401`, stop. The console is not gated and must not be
published.

## Deploy

```bash
brew install flyctl        # or: curl -L https://fly.io/install.sh | sh
flyctl auth login
flyctl launch --no-deploy --copy-config --name anvil-rohanbatra
```

Set the one secret. It goes in Fly's encrypted secret store, never in
`fly.toml`:

```bash
flyctl secrets set ANVIL_CONSOLE_PASSWORD="$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))')"
flyctl secrets list        # confirms the name, never the value
flyctl deploy
```

Verify there is nothing else in there:

```bash
flyctl secrets list        # ANVIL_CONSOLE_PASSWORD and nothing else
```

## The domain

```bash
flyctl ips allocate-v4 --shared
flyctl ips allocate-v6
flyctl certs create anvil.rohanbatra.in
flyctl certs show anvil.rohanbatra.in     # prints the records to add
```

At your DNS provider for `rohanbatra.in`:

| Type | Name | Value |
|---|---|---|
| `A` | `anvil` | the IPv4 from `flyctl ips list` |
| `AAAA` | `anvil` | the IPv6 from `flyctl ips list` |

Fly issues the certificate automatically once the records resolve — usually a
few minutes, occasionally an hour. `flyctl certs show` reports progress.

If you use Cloudflare, set the records to **DNS only** (grey cloud) until the
certificate issues. Proxying before issuance makes the ACME challenge fail in a
way that is tedious to diagnose.

## Continuous deployment

`.github/workflows/deploy.yml` deploys on every green CI run on `main`. It needs
one repository secret:

```bash
flyctl tokens create deploy -x 8760h
# GitHub → Settings → Secrets and variables → Actions → New repository secret
#   Name:  FLY_API_TOKEN
#   Value: the token
```

The workflow will not deploy a red build, and after deploying it checks three
things: that `/health` returns 200, that `/` returns **401** (the console is
gated), and that the instance reports `offline` mode. Any of those failing fails
the deploy.

---

## Post-deploy checklist

Run through this once, and again before you send the link to anyone.

**It works**

- [ ] `https://anvil.rohanbatra.in/health` returns 200 with `"mode":"offline"`
- [ ] `/` prompts for credentials
- [ ] The credentials you intend to share actually work
- [ ] Approval inbox lists paused cases; approving one resumes the graph
- [ ] Recovery cockpit renders the batch, including the honest limitations
- [ ] Retry scheduler responds to a changed failure date
- [ ] Policy evaluator returns a trace
- [ ] Classifier: `U30` resolves, `switch busy` escalates
- [ ] Ledger postings balance
- [ ] `/docs` renders the OpenAPI

**It is safe**

- [ ] `flyctl secrets list` shows `ANVIL_CONSOLE_PASSWORD` and nothing else
- [ ] `/health` reports `offline`
- [ ] `curl https://anvil.rohanbatra.in/robots.txt` disallows everything
- [ ] Security headers present: CSP, HSTS, `X-Frame-Options`, `nosniff`
- [ ] Thirteen rapid `/api/batch` calls produce a `429`
- [ ] `git log -p | grep -i "rzp_test\|sk-ant"` finds nothing
- [ ] The repository contains no `.env`

**It presents well**

- [ ] Custom domain resolves and the certificate is valid
- [ ] Dark and light themes both render correctly
- [ ] It is usable on a laptop screen without horizontal scrolling
- [ ] The README links to the live URL and states the credentials
- [ ] `REVIEWING.md` routes a reviewer through it in fifteen minutes

## Rollback

```bash
flyctl releases                    # list
flyctl releases rollback <version>
```

## Watching it

```bash
flyctl logs           # structured JSON, redacted at the boundary
flyctl status
flyctl dashboard
```

Logs pass through the redaction processor in `anvil/core/logging.py`, so a
sensitive key cannot reach the aggregator even if something tries to log one.

---

## Later: webhooks against live Razorpay

**Local only.** Do not point Razorpay at the public instance — it has no webhook
secret and no credentials, and giving it any would undo the entire posture above.

```bash
# terminal 1
ANVIL_MODE=live make console

# terminal 2
ngrok http 8000
```

Set the webhook URL in the Razorpay dashboard to
`https://<your-ngrok-host>/webhooks/razorpay` with the same secret you put in
`.env`, and subscribe to `payment.failed`, `payment.captured`,
`payment.authorized`, `subscription.charged`, `subscription.pending` and
`subscription.halted`.

Send a test webhook and confirm a `200`. Then change one character of the secret
and confirm a `400` — a verifier that never rejects anything is not verifying.
