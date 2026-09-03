# Security

Anvil is a demonstration system built for the Razorpay AI Buildathon 2026. It is
**not deployed anywhere and holds no real customer data**, but it is written as
though it were, because a payments system that is only careful in production is
not careful.

## Reporting a vulnerability

Open a private security advisory through GitHub's *Security → Report a
vulnerability* flow, or email the maintainer listed in `pyproject.toml`. Please
do not open a public issue for anything exploitable.

Expect an acknowledgement within 72 hours. There is no bounty programme.

## What this codebase does with sensitive data

Worth stating plainly, because "we take security seriously" is not a security
posture and the specifics are checkable.

**Card numbers are never stored.** No field in the 33-table schema holds a PAN.
`anvil/llm/redaction.py` detects PANs using the Luhn algorithm specifically so
that a genuine card number is redacted while an arbitrary sixteen-digit number
is not — precision matters, because a redactor that fires on everything trains
people to ignore it.

**Contact details are tokenised.** `customers` stores an irreversible pseudonym
plus a display-safe hint (`•••4821`). That is enough for an operator to
recognise someone and never enough to contact them out of band or to leak them
through a log.

**Logs redact on write, not on read.** `anvil/core/logging.py` masks a fixed set
of sensitive keys inside a structlog processor, so a careless
`log.info("charging", vpa=customer.vpa)` cannot leak a payment identifier into
an aggregator. Redacting on read would mean the raw value had already been
written somewhere.

**The audit trail is PII-free by construction.** Invariant 10. Redaction happens
before persistence and the writer refuses records that still match a PII
pattern, so a leak is a failed write rather than a discovered-later problem.

**Nothing reaches a model unredacted.** Prompts pass through the redaction layer
before leaving the process, and the recorded prompt on `llm_calls` is the
redacted form.

**Secrets come from the environment only.** `anvil/core/config.py` holds them as
Pydantic `SecretStr`, and `.env` is gitignored. Offline mode — the default —
requires no credentials at all, so a clone can be run without ever handling one.

## Deliberate non-goals

There is no authentication on the console API. It binds to localhost, serves a
seeded simulator, and holds nothing real. Adding a login screen would imply a
threat model this demonstration does not have. **Do not expose it to a network
without putting real authentication in front of it.**

## Cryptography

HMAC-SHA256 webhook verification uses `hmac.compare_digest`, and the comparison
is made against the **raw request bytes** rather than a re-serialised payload —
re-serialisation changes the byte sequence and guarantees a mismatch. Step-up
challenges store a salted digest, never the code itself.
