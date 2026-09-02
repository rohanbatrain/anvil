"""Making the append-only rule structural.

Invariant 1 says balances are never mutated and invariant 2 says transactions
balance. Both are enforced in :mod:`anvil.ledger.posting`, but application-level
enforcement protects only the paths that go through the application. A migration
that "just fixes one row", a psql session at 2am, or a future module that adds
its own writer would all bypass it.

So the ledger tables refuse mutation at the database level. ``UPDATE`` and
``DELETE`` on ``ledger_entries`` and ``ledger_transactions`` raise an exception
inside Postgres itself. There is no application code path, and no direct SQL
session, that can quietly rewrite financial history.

The one deliberate escape hatch is ``anvil.allow_ledger_mutation``, a session
GUC that the guard checks. Nothing in Anvil ever sets it. It exists because a
genuine disaster-recovery operation must be *possible* -- an immutability rule
with no documented override gets dropped in a panic, which is strictly worse
than one that must be turned on explicitly, leaves the intent in the session
settings, and can be alerted on.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

#: Applied by the migration. Idempotent: safe to run repeatedly.
LEDGER_IMMUTABILITY_DDL = """
CREATE OR REPLACE FUNCTION anvil_reject_ledger_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF current_setting('anvil.allow_ledger_mutation', true) = 'on' THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    RAISE EXCEPTION
        'ledger is append-only: % on % is refused. Post a reversal instead.',
        TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation',
              HINT = 'Corrections are made by posting a mirrored REVERSAL '
                     'transaction that references the original, never by editing it.';
END;
$$;

DROP TRIGGER IF EXISTS trg_ledger_entries_immutable ON ledger_entries;
CREATE TRIGGER trg_ledger_entries_immutable
    BEFORE UPDATE OR DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION anvil_reject_ledger_mutation();

DROP TRIGGER IF EXISTS trg_ledger_transactions_immutable ON ledger_transactions;
CREATE TRIGGER trg_ledger_transactions_immutable
    BEFORE UPDATE OR DELETE ON ledger_transactions
    FOR EACH ROW EXECUTE FUNCTION anvil_reject_ledger_mutation();

DROP TRIGGER IF EXISTS trg_domain_events_immutable ON domain_events;
CREATE TRIGGER trg_domain_events_immutable
    BEFORE UPDATE OR DELETE ON domain_events
    FOR EACH ROW EXECUTE FUNCTION anvil_reject_ledger_mutation();

DROP TRIGGER IF EXISTS trg_audit_records_immutable ON audit_records;
CREATE TRIGGER trg_audit_records_immutable
    BEFORE UPDATE OR DELETE ON audit_records
    FOR EACH ROW EXECUTE FUNCTION anvil_reject_ledger_mutation();
"""

LEDGER_IMMUTABILITY_DOWN_DDL = """
DROP TRIGGER IF EXISTS trg_ledger_entries_immutable ON ledger_entries;
DROP TRIGGER IF EXISTS trg_ledger_transactions_immutable ON ledger_transactions;
DROP TRIGGER IF EXISTS trg_domain_events_immutable ON domain_events;
DROP TRIGGER IF EXISTS trg_audit_records_immutable ON audit_records;
DROP FUNCTION IF EXISTS anvil_reject_ledger_mutation();
"""

#: The tables the guard protects, in the order a reader should think about them.
PROTECTED_TABLES: tuple[str, ...] = (
    "ledger_entries",
    "ledger_transactions",
    "domain_events",
    "audit_records",
)


async def apply(connection: AsyncConnection) -> None:
    """Install the guard. Used by the migration and by the test fixtures."""
    from sqlalchemy import text

    await connection.execute(text(LEDGER_IMMUTABILITY_DDL))


async def remove(connection: AsyncConnection) -> None:
    from sqlalchemy import text

    await connection.execute(text(LEDGER_IMMUTABILITY_DOWN_DDL))
