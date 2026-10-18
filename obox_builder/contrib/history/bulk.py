"""ORM-friendly bulk helper: every touched row still produces a history entry."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession


async def bulk_update(
    db: AsyncSession,
    model: type[Any],
    values: Mapping[str, Any],
    *,
    where: Any = None,
) -> int:
    """Update every matching row through the unit of work (audit-safe).

    ``sa.update()`` statements bypass the ORM flush and therefore bypass row
    history (``HISTORY_BULK_POLICY`` only warns/raises). This helper loads the
    matching rows, assigns ``values`` and flushes once, so each changed row
    gets its own entry. Returns the number of visited rows. The caller owns
    the commit.
    """
    statement = sa.select(model)
    if where is not None:
        statement = statement.where(where)
    pk = sa.inspect(model).primary_key[0]
    rows = list(await db.scalars(statement.order_by(pk)))
    for row in rows:
        for name, value in values.items():
            setattr(row, name, value)
    await db.flush()
    return len(rows)
