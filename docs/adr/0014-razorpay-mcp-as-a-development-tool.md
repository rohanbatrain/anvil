# ADR-0014: Razorpay's MCP server is a development tool, not the product's gateway

- **Status:** Accepted
- **Date:** 2026-09-03

## Context

Razorpay publishes a hosted MCP server at `https://mcp.razorpay.com/mcp`
exposing around fifty tools — payments, orders, payment links, refunds, QR
codes, settlements, payouts and tokens — authenticated with
`Authorization: Basic base64(key_id:key_secret)`.

The obvious question is whether Anvil should call Razorpay *through* it and
delete `anvil/gateway/`.

Two facts decide it.

**The MCP server does not cover our domain.** Razorpay's own tools reference
lists no subscription or plan tools. Anvil is a recurring-payments system;
creating a plan, creating a subscription and charging a recurring invoice are its
centre of gravity, and none of the three is available. What *is* available and
genuinely useful is `create_registration_link` — which is the mandate
authorisation flow — plus `fetch_tokens`, `revoke_token`, orders, payments,
payment links and refunds.

**An MCP tool call is not a payment integration.** Anvil's gateway carries a
caller-generated idempotency key on every mutating call, separates connect from
read timeouts, distinguishes retryable from non-retryable errors, and treats a
timeout as an *unknown outcome* requiring reconciliation rather than a failure
(ADR-0009). Those properties are the difference between a payment client and an
HTTP call, and none of them is expressible through a tool invocation whose
retry semantics belong to somebody else's client.

## Decision

The Razorpay MCP server is configured as a **development-time instrument**, in
`.mcp.json`, for exploring the API, creating test-mode fixtures and checking
Anvil's own client against real responses.

**`anvil/gateway/` remains the product's integration**, and keeps its
idempotency keys, timeout policy and reconciliation path.

Credentials never enter the repository: `.mcp.json` interpolates
`${RAZORPAY_MCP_TOKEN}` from the environment, and `scripts/mcp_token.py` derives
that token from `.env` through Anvil's settings, so the secret is never typed on
a command line where shell history would keep it.

## Consequences

Exploration gets much cheaper. Fetching a real payment's shape to check a parser
against becomes a question rather than a script.

Recording offline fixtures from genuine responses becomes straightforward, which
matters: hand-written fixtures prove nothing about whether a real response
validates.

`scripts/mcp_token.py` **refuses any key that is not `rzp_test_`**. Anvil moves
money, the entire point of the demonstration is that it never moves anybody's
real money, and a warning is not a control.

We carry a configuration file for a dependency the product does not use at
runtime, which will confuse someone eventually. This ADR is the answer to that
confusion.

## Alternatives considered

**Route Anvil's gateway through MCP.** Rejected on both grounds above: it lacks
the subscription surface we need, and it would surrender idempotency and timeout
semantics to a layer that does not model them.

**Run the MCP server locally** (`razorpay/razorpay-mcp-server` via Docker or Go).
Rejected because Docker is unavailable on this machine (ADR-0013) and the hosted
server needs no infrastructure.

**Skip MCP entirely and use the REST API directly.** Still what the product
does. Adding MCP alongside costs one config file and makes the exploratory loop
much faster, so there was no reason to choose between them.
