"""ledger immutability guard

Installs the triggers that make the append-only rule a database refusal rather
than an application convention. See anvil/ledger/immutability.py for why.

Revision ID: 9a1b2c3d4e5f
Revises: 8c4dce6e89c7
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from anvil.ledger.immutability import (
    LEDGER_IMMUTABILITY_DDL,
    LEDGER_IMMUTABILITY_DOWN_DDL,
)

revision: str = "9a1b2c3d4e5f"
down_revision: str | None = "8c4dce6e89c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(LEDGER_IMMUTABILITY_DDL)


def downgrade() -> None:
    op.execute(LEDGER_IMMUTABILITY_DOWN_DDL)
