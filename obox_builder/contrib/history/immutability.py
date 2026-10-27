"""Append-only enforcement: DB triggers that forbid mutating history rows.

Installed by a ``post_migrate`` receiver (same mechanism as
``sync_permissions``, idempotent). PostgreSQL blocks ``UPDATE``/``DELETE`` and
``TRUNCATE`` unless ``obox.history_maintenance`` is ``'on'`` (set locally by
``history_purge``); SQLite blocks ``UPDATE`` only so the dev/test purge path
keeps working. The real security boundary is DB roles (``REVOKE`` executed by
the operator); the trigger is the accident/bug guardrail.
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("obox.history")

TABLE = "obox_row_change"

#: Local switch that lets maintenance commands (purge, anonymize) mutate rows.
MAINTENANCE_GUC = "SET LOCAL obox.history_maintenance = 'on'"

#: SQLite maintenance path: dropped before maintenance updates, recreated after.
SQLITE_DROP_TRIGGER = f"DROP TRIGGER IF EXISTS {TABLE}_immutable"
SQLITE_CREATE_TRIGGER = (
    f"CREATE TRIGGER {TABLE}_immutable BEFORE UPDATE ON {TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{TABLE} is append-only (UPDATE)'); END"
)

_PG_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION history_forbid_mutation() RETURNS trigger AS $$
BEGIN
  IF current_setting('obox.history_maintenance', true) = 'on' THEN
    RETURN COALESCE(NEW, OLD);
  END IF;
  RAISE EXCEPTION '{TABLE} is append-only (%)', TG_OP USING ERRCODE = '42501';
END $$ LANGUAGE plpgsql
"""


def statements(dialect: str) -> list[str]:
    """The DDL for ``dialect`` (idempotent: drops then recreates triggers)."""
    if dialect == "postgresql":
        return [
            _PG_FUNCTION_SQL,
            f"DROP TRIGGER IF EXISTS {TABLE}_immutable ON {TABLE}",
            (
                f"CREATE TRIGGER {TABLE}_immutable BEFORE UPDATE OR DELETE ON {TABLE} "
                "FOR EACH ROW EXECUTE FUNCTION history_forbid_mutation()"
            ),
            f"DROP TRIGGER IF EXISTS {TABLE}_no_truncate ON {TABLE}",
            (
                f"CREATE TRIGGER {TABLE}_no_truncate BEFORE TRUNCATE ON {TABLE} "
                "FOR EACH STATEMENT EXECUTE FUNCTION history_forbid_mutation()"
            ),
        ]
    if dialect == "sqlite":
        return [SQLITE_DROP_TRIGGER, SQLITE_CREATE_TRIGGER]
    return []


async def install(db: AsyncSession) -> bool:
    """(Re)create the append-only triggers; ``False`` when unsupported."""
    ddl = statements(db.get_bind().dialect.name)
    for statement in ddl:
        await db.execute(sa.text(statement))
    return bool(ddl)


# -- maintenance sessions (purge, anonymize, relabel) -------------------------


async def begin_maintenance(db: AsyncSession) -> str | None:
    """Suspend the append-only guard; return the mode for the helpers below."""
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        return "postgresql"
    if dialect == "sqlite":
        await db.execute(sa.text(SQLITE_DROP_TRIGGER))
        return "sqlite"
    return None


async def per_transaction_maintenance(db: AsyncSession, mode: str | None) -> None:
    """Per-transaction maintenance switch on PostgreSQL (``SET LOCAL``)."""
    if mode == "postgresql":
        await db.execute(sa.text(MAINTENANCE_GUC))


async def end_maintenance(db: AsyncSession, mode: str | None) -> None:
    """Re-create the SQLite trigger; PostgreSQL needs no cleanup."""
    if mode == "sqlite":
        await db.execute(sa.text(SQLITE_CREATE_TRIGGER))
        await db.commit()


async def install_trigger_receiver(sender: object, **kwargs: object) -> None:
    """``post_migrate`` receiver: install the append-only triggers."""
    from obox_builder.contrib.history.models import RowChange
    from obox_builder.db.router import router
    from obox_builder.db.session import session_scope

    database = str(kwargs.get("database", "default"))
    if not router.allow_migrate_model(database, RowChange):
        return
    async with session_scope(database) as db:
        if await install(db):
            logger.info("Installed append-only trigger on %s.", TABLE)
