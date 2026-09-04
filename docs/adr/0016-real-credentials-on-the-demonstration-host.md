# ADR-0016: The demonstration host holds real test-mode credentials

- **Status:** Accepted
- **Date:** 2026-09-04
- **Supersedes:** the credential posture in ADR-0015 and the deployment guide

## Context

The deployment was first designed to hold **no payment credentials at all**. The
public instance would run offline against the seeded simulator, so it could not
leak a key — there would be none — and could not move money.

That reasoning was sound about leaks and wrong about the demonstration.

The submission is assessed by a payments company. The single most checkable
claim in the repository is that inbound webhooks are verified correctly: HMAC
over the **raw bytes**, a replay window, and dedupe on the event id. A reviewer
can verify that against Razorpay's own system in a way they cannot verify
anything else. An instance with no credentials cannot demonstrate it, and the
alternative — an ngrok tunnel on a developer laptop — will not be running when
somebody opens the link.

The risk was also mis-weighted. These are **test-mode** keys. A leaked
`rzp_test_` key can create test orders. It cannot move anybody's money. That is
a materially different exposure from the one the original posture was defending
against.

## Decision

The demonstration host holds real credentials: Razorpay test-mode key id and
secret, the webhook secret, and an Anthropic API key.

This is made defensible by a control rather than by care:

**Anvil refuses to start against a production Razorpay key.** A field validator
on `razorpay_key_id` raises unless it begins `rzp_test_`. The instance cannot be
pointed at production Razorpay by mistake, by a bad paste, or by anyone with
write access to the environment file.

Supporting measures: credentials live only in `/etc/anvil/anvil.env` at mode
`640`, root-owned and readable by the service account alone — never in git,
never in an image, never in the world-readable unit file. `/health` reports the
*kind* of key rather than the key. The deploy script and CI both verify the
running instance reports test mode, so the control is enforced at startup and
checked again afterwards. The console is credential-gated. A one-line kill
switch returns the instance to the simulator.

## Consequences

The demonstration can do the thing it exists to do: receive a real webhook from
Razorpay, verify a real signature, and reject a tampered one.

The Anthropic key becomes the credential that actually warrants care, because it
is the only one on the host that can cost real money. A spend limit on the key
is the mitigation, and the deployment guide says so.

There is now a real credential on a public machine, which is a genuine increase
in exposure over the original design. The honest accounting is that the worst
case moved from *nothing* to *somebody creates test orders and spends some
Anthropic credit*, in exchange for a demonstration that shows the integration
rather than describing it.

Everything must be rotated after the demonstration period. This is written down
because it is exactly the kind of task that does not happen unless it is.

## Alternatives considered

**Keep the host credential-free; demonstrate webhooks over ngrok during the
pitch.** The original plan. Rejected because the tunnel is not running when a
reviewer opens the link on their own time, which is when most of the assessment
happens.

**Credential-free host, with a recorded video of the webhook flow.** Weaker: a
recording is an assertion, and the whole posture of this project is that claims
should be checkable.

**Hold the credentials in a secrets manager rather than a file.** Correct at
scale and disproportionate here. A root-owned `640` file on a single-tenant host
has a comparable exposure to a secrets-manager credential that the same host
must hold in memory anyway, and it adds a dependency and a failure mode for the
demonstration to trip over.
