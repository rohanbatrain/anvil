"""Print the Basic auth token the Razorpay MCP server expects.

The remote server authenticates with ``Authorization: Basic <token>`` where the
token is ``base64(key_id:key_secret)``. This reads the credentials from the
environment or ``.env`` through Anvil's own settings, so the secret is never
typed on a command line — where it would land in shell history — and never
written into a checked-in file.

Usage::

    export RAZORPAY_MCP_TOKEN=$(.venv/bin/python scripts/mcp_token.py)

``.mcp.json`` interpolates that variable, so the repository holds the shape of
the configuration and none of the secret.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anvil.core.config import Settings


def main() -> int:
    settings = Settings()
    key_id = settings.razorpay_key_id
    key_secret = settings.razorpay_key_secret.get_secret_value()

    if not key_id or not key_secret:
        missing = [
            name
            for name, value in (
                ("ANVIL_RAZORPAY_KEY_ID", key_id),
                ("ANVIL_RAZORPAY_KEY_SECRET", key_secret),
            )
            if not value
        ]
        print(
            "Cannot build the MCP token: " + ", ".join(missing) + " not set.\n"
            "Add them to .env (which is gitignored). See "
            "docs/how-to/connect-razorpay-test-mode.md",
            file=sys.stderr,
        )
        return 1

    if not key_id.startswith("rzp_test_"):
        # Refusing rather than warning. Anvil moves money, and the whole point
        # of the demonstration is that it never moves anybody's real money.
        print(
            f"Refusing: {key_id[:12]}… is not a test-mode key. Anvil is only ever "
            "run against rzp_test_ credentials.",
            file=sys.stderr,
        )
        return 1

    print(base64.b64encode(f"{key_id}:{key_secret}".encode()).decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
