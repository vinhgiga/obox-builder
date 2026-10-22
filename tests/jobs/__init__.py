from __future__ import annotations

from typing import Any

from obox_builder.db.session import session_scope


async def read(statement: Any) -> Any:
    async with session_scope() as db:
        return await db.scalar(statement)
