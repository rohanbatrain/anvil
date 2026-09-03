# How to deploy the public demonstration

Target: **https://anvil.rohanbatra.in**, on a VPS running Ubuntu 22.04 or 24.04.

## The security posture, first

Everything below follows from one decision, and it is worth understanding before
running any of it.

**The public instance holds no payment credentials at all.** It runs in offline
mode against the seeded simulator. It cannot leak a Razorpay key because there
is none on the machine, and it cannot move money because nothing is wired to a
payment API. The only secret it holds is the console password, which protects
the demonstration and nothing else.

Live mode and webhooks stay **local, behind a temporary tunnel**, only while you
are testing. A permanently public endpoint holding real credentials is a
liability with no upside for a demonstration.

Both `deploy/deploy.sh` and the CI workflow **fail the deploy** if the running
instance reports anything other than `offline`.

| | Local `.env` | The VPS |
|---|---|---|
| Razorpay key id / secret | yes, for testing | **never** |
| Razorpay webhook secret | yes, for testing | **never** |
| Anthropic API key | yes | **never** |
| `ANVIL_CONSOLE_PASSWORD` | not needed | **yes** — the only secret |
| `ANVIL_MODE` | `live` while testing | `offline` |

## What gets installed, and why

| Piece | Choice | Why |
|---|---|---|
| Process supervision | **systemd**, not Docker | Fewer moving parts, and Docker has failed twice on this project. `journalctl -u anvil` is the whole story. |
| TLS and reverse proxy | **Caddy**, not nginx + certbot | Caddy obtains and renews the certificate itself. No cron job to forget, no renewal that fails silently in three months. |
| Binding | **127.0.0.1 only** | Caddy proxies over loopback. The application is unreachable from the network even if the firewall is wrong — a service protected only by a firewall rule is protected by one mistake. |
| Releases | **timestamped directories, atomic symlink** | A failed build leaves the running version untouched. Rollback is a symlink swap. |

---

## 1. Prepare the server

SSH in as root (or with sudo) and run the bootstrap once:

```bash
git clone https://github.com/rohanbatrain/anvil.git /tmp/anvil
sudo bash /tmp/anvil/deploy/bootstrap.sh
```

Override the domain if it differs:

```bash
sudo ANVIL_DOMAIN=anvil.example.com bash /tmp/anvil/deploy/bootstrap.sh
```

It is idempotent — safe to re-run after a change. What it does:

- installs Python, build tools, Caddy, `ufw`, `fail2ban` and unattended security
  upgrades
- creates a **service account** `anvil` with no shell and no home, which exists
  to own a process rather than to be used
- creates a **deploy account** `deploy` whose passwordless sudo is scoped to two
  commands — `systemctl restart anvil` and `systemctl status anvil`. A
  compromised CI token can restart a service; it cannot own the box.
- creates `/etc/anvil/anvil.env` mode `640`, root-owned, readable by the service
  account, populated with the offline defaults
- installs the systemd unit and the Caddy site
- **firewall: 22, 80, 443 only.** Port 8000 is deliberately not opened.
- **SSH: keys only, no root login,** three auth attempts

## 2. Set the console password

The one secret on the box. It is never in the repository and never in the unit
file, which is world-readable.

```bash
sudo sed -i "s/CHANGE_ME/$(openssl rand -base64 24 | tr -d '/+=')/" /etc/anvil/anvil.env
sudo grep CONSOLE_PASSWORD /etc/anvil/anvil.env      # note it down
```

That value goes in your submission so reviewers can get in.

## 3. Point DNS at the server

At your DNS provider for `rohanbatra.in`:

| Type | Name | Value |
|---|---|---|
| `A` | `anvil` | the server's IPv4 |
| `AAAA` | `anvil` | the server's IPv6, if it has one |

`curl -s4 ifconfig.me` on the server prints the IPv4.

Caddy obtains the certificate automatically once the record resolves — usually
under a minute, occasionally longer. Until then it retries and logs the failure,
which is the expected state rather than a fault.

**Using Cloudflare?** Set the record to **DNS only** (grey cloud) until the
certificate issues. Proxying beforehand makes the ACME challenge fail in a way
that is tedious to diagnose.

## 4. First deploy

```bash
sudo -u deploy bash /opt/anvil/repo/deploy/deploy.sh
```

The script builds a release, installs dependencies, and then — before promoting
anything — **runs the unit suite and the guided tour against the new code**. A
release whose own tests fail is never promoted. Only then does
`/opt/anvil/current` move, and the service restart follows.

If the health check does not pass within a minute it **rolls back to the
previous release by itself** and exits non-zero.

## 5. Continuous deployment

`.github/workflows/deploy.yml` runs on every green CI run on `main`. It opens an
SSH session and runs the same `deploy/deploy.sh` a human would — one code path,
so the manual and automated routes cannot drift.

Generate a key for CI, on your machine:

```bash
ssh-keygen -t ed25519 -C "github-actions-anvil" -f ~/.ssh/anvil_deploy -N ""
cat ~/.ssh/anvil_deploy.pub     # the public half
```

Authorise it on the server:

```bash
echo 'ssh-ed25519 AAAA... github-actions-anvil' \
  | sudo tee -a /home/deploy/.ssh/authorized_keys
```

Capture the host key so CI can pin it rather than accepting whatever answers:

```bash
ssh-keyscan -H anvil.rohanbatra.in 2>/dev/null
```

Then add four **repository secrets** under GitHub → Settings → Secrets and
variables → Actions:

| Secret | Value |
|---|---|
| `DEPLOY_SSH_KEY` | the whole private key, `~/.ssh/anvil_deploy` |
| `DEPLOY_KNOWN_HOSTS` | the `ssh-keyscan` output |
| `DEPLOY_HOST` | `anvil.rohanbatra.in` |
| `DEPLOY_USER` | `deploy` |

`DEPLOY_KNOWN_HOSTS` is not optional politeness: without it the workflow would
need `StrictHostKeyChecking=no`, which accepts whatever answers and turns a DNS
hijack into a shell on your server.

---

## Post-deploy checklist

Run this once, and again before sending the link to anyone.

**It works**

- [ ] `https://anvil.rohanbatra.in/health` returns 200 with `"mode":"offline"`
- [ ] `/` prompts for credentials
- [ ] The credentials you intend to share actually work
- [ ] Approval inbox lists paused cases; approving one resumes the graph
- [ ] Recovery cockpit renders the batch, including the honest limitations
- [ ] Retry scheduler responds to a changed failure date
- [ ] Policy evaluator returns a rule-by-rule trace
- [ ] Classifier: `U30` resolves, `switch busy` escalates
- [ ] Ledger postings balance
- [ ] `/docs` renders the OpenAPI

**It is safe**

- [ ] `sudo grep -c RAZORPAY /etc/anvil/anvil.env` returns `0`
- [ ] `sudo grep -c ANTHROPIC /etc/anvil/anvil.env` returns `0`
- [ ] `sudo ss -tlnp | grep 8000` shows `127.0.0.1:8000`, **not** `0.0.0.0:8000`
- [ ] `sudo ufw status` allows only 22, 80, 443
- [ ] `curl https://anvil.rohanbatra.in/robots.txt` disallows everything
- [ ] Headers present: HSTS, CSP, `X-Frame-Options`, `nosniff`
- [ ] Thirteen rapid `/api/batch` calls produce a `429`
- [ ] `curl -sI http://<server-ip>:8000/` from your laptop **fails to connect**
- [ ] `sudo -u deploy sudo -l` lists only the two systemctl commands
- [ ] `git log -p | grep -iE "rzp_(test|live)_|sk-ant-"` finds nothing real
- [ ] `ssh root@…` is refused

**It presents well**

- [ ] The certificate is valid and the padlock is clean
- [ ] Dark and light themes both render
- [ ] Usable on a laptop without horizontal scrolling
- [ ] The README states the live URL and the credentials
- [ ] `REVIEWING.md` routes a reviewer through it in fifteen minutes

---

## Operating it

```bash
sudo systemctl status anvil
sudo journalctl -u anvil -f              # structured JSON, redacted at source
sudo journalctl -u anvil -n 200 --no-pager
sudo systemctl restart anvil
sudo systemctl reload caddy              # after editing /etc/caddy/Caddyfile
sudo journalctl -u caddy -n 50           # certificate problems show up here
```

Logs pass through the redaction processor in `anvil/core/logging.py`, so a
sensitive key cannot reach the journal even if something tries to log one.

### Rollback

```bash
sudo -u deploy bash /opt/anvil/repo/deploy/rollback.sh                 # previous
ls -1 /opt/anvil/releases                                              # pick one
sudo -u deploy bash /opt/anvil/repo/deploy/rollback.sh 20260903-101500-a1b2c3
```

The last five releases are kept on disk, so a rollback needs no network.

### When something is wrong

| Symptom | Look at |
|---|---|
| 502 from Caddy | `journalctl -u anvil -n 80` — the app is down or still starting |
| Certificate never issues | `journalctl -u caddy -n 50`, then check DNS resolves and 80 is open |
| 401 with the right password | `sudo grep CONSOLE /etc/anvil/anvil.env`, then restart |
| Console open with no prompt | `ANVIL_CONSOLE_PASSWORD` is unset — the startup log warns about this |
| Deploy rolls itself back | `journalctl -u anvil -n 80`; the new release failed its health check |
| Out of memory | `MemoryMax=768M` in the unit; the batch is the heavy part |

---

## Later: webhooks against live Razorpay

**Local only.** Do not point Razorpay at the VPS — it has no webhook secret and
no credentials, and giving it any would undo the entire posture above.

```bash
# terminal 1
ANVIL_MODE=live make console

# terminal 2
ngrok http 8000
```

Set the webhook URL in the Razorpay dashboard to
`https://<your-ngrok-host>/webhooks/razorpay`, with the same secret you put in
`.env`, and subscribe to `payment.failed`, `payment.captured`,
`payment.authorized`, `subscription.charged`, `subscription.pending` and
`subscription.halted`.

Send a test webhook and confirm a `200`. Then change one character of the secret
and confirm a `400` — a verifier that never rejects anything is not verifying.
