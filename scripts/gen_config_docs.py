"""Generate `docs/reference/configuration.md` from the settings model.

A configuration reference maintained by hand drifts the first time somebody adds
a field and forgets the docs, and a reference that is wrong is worse than none.
Generating it means the page cannot disagree with the code.

Run after changing `anvil/core/config.py`::

    .venv/bin/python scripts/gen_config_docs.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anvil.core.config import Settings

OUT = Path(__file__).resolve().parent.parent / "docs" / "reference" / "configuration.md"

#: Grouped for reading rather than alphabetically. A reader looking for the
#: credentials should not have to scan past the connection pool settings.
GROUPS: dict[str, list[str]] = {
    "Runtime": ["MODE", "ENV", "LOG_LEVEL", "LOG_FORMAT", "SEED"],
    "Infrastructure": [
        "DATABASE_URL",
        "REDIS_URL",
        "DB_POOL_SIZE",
        "DB_MAX_OVERFLOW",
        "DB_STATEMENT_TIMEOUT_MS",
    ],
    "Live-mode credentials": [
        "RAZORPAY_KEY_ID",
        "RAZORPAY_KEY_SECRET",
        "RAZORPAY_WEBHOOK_SECRET",
        "ANTHROPIC_API_KEY",
    ],
    "Public deployment": ["CONSOLE_USERNAME", "CONSOLE_PASSWORD", "PUBLIC_BASE_URL"],
    "Models": ["MODEL_PLANNER", "MODEL_CLASSIFIER", "MODEL_COMPOSER"],
    "Guardrails": [
        "WEBHOOK_TOLERANCE_SECONDS",
        "LLM_MAX_RETRIES",
        "LLM_TIMEOUT_SECONDS",
        "LLM_MAX_OUTPUT_TOKENS",
    ],
    "Paths": ["FIXTURES_DIR"],
}

PREAMBLE = """# Configuration

Every setting, its default and what it does. All are read from the environment
with an `ANVIL_` prefix, or from a `.env` file in the working directory.

**Every value has a working default.** Offline mode — the default — needs no
credentials at all, which is what lets a clone be run without ever handling one.

This page is generated from `anvil/core/config.py` by
`scripts/gen_config_docs.py`, so it cannot drift from the model. Settings are
held frozen and secrets are `SecretStr`, so they do not appear in a traceback or
a log line.
"""

EPILOGUE = """
## Two environments, two different files

This distinction matters more than any individual value below. See
[the deployment guide](../how-to/deploy.md).

| | Local `.env` | Deployed host |
|---|---|---|
| Razorpay and Anthropic credentials | yes, for live testing | **never** |
| `ANVIL_CONSOLE_PASSWORD` | not needed | **yes** — the only secret there |
| `ANVIL_MODE` | `live` while testing | `offline` |

The public instance holds no payment credentials, so it cannot leak one and
cannot move money. The deploy workflow fails the build if the deployed instance
reports anything other than offline mode.

## Validation at startup

`ANVIL_MODE=live` **fails fast** if any of the four live-mode credentials is
missing, naming exactly which ones. Discovering a missing webhook secret on the
first inbound webhook is worse than not starting.

`ANVIL_SEED` must be positive. A seed of zero is still reproducible but reads as
"unset" to anyone auditing a batch.

Leaving `ANVIL_CONSOLE_PASSWORD` unset logs a warning, because an
unauthenticated console is correct on localhost and wrong on a public host.

## Derived values

`sync_database_url` rewrites the asyncpg URL for psycopg, which Alembic and the
LangGraph Postgres checkpointer need. `raw_database_url` strips the driver for
libraries that build their own connection. Neither is set directly — configure
`ANVIL_DATABASE_URL` and the rest follows.
"""


def render() -> str:
    fields = Settings.model_fields
    lines: list[str] = [PREAMBLE]

    seen: set[str] = set()
    for group, names in GROUPS.items():
        lines.append(f"## {group}\n")
        lines.append("| Variable | Default | Notes |")
        lines.append("|---|---|---|")
        for name in names:
            key = name.lower()
            if key not in fields:
                raise SystemExit(f"{name} is grouped but not in Settings")
            seen.add(key)
            field = fields[key]
            default = field.default
            if hasattr(default, "get_secret_value"):
                default = "(unset)"
            default = str(default)
            if len(default) > 44:
                default = default[:41] + "…"
            lines.append(f"| `ANVIL_{name}` | `{default}` | {field.description or ''} |")
        lines.append("")

    # A field nobody grouped would otherwise vanish from the reference silently.
    ungrouped = sorted(set(fields) - seen)
    if ungrouped:
        raise SystemExit(
            "these settings are not in any group and would be undocumented: " + ", ".join(ungrouped)
        )

    lines.append(EPILOGUE)
    return "\n".join(lines)


if __name__ == "__main__":
    OUT.write_text(render(), encoding="utf-8")
    print(f"wrote {OUT.relative_to(Path.cwd())}")
