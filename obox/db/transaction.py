"""Asynchronous transactions with Django's atomic context-manager contract."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from obox.db import _current, _session


@asynccontextmanager
async def _savepoint(session: AsyncSession) -> AsyncIterator[None]:
    connection = await session.connection()
    if connection.dialect.name == "sqlite":
        raw = await connection.get_raw_connection()
        # Legacy SQLite drivers do not begin a transaction before SAVEPOINT.
        driver = raw.driver_connection
        if driver is not None and not driver.in_transaction:
            await connection.exec_driver_sql("BEGIN")
    async with session.begin_nested():
        yield


@asynccontextmanager
async def atomic(
    using: str | None = None, savepoint: bool = True, durable: bool = False
) -> AsyncIterator[None]:
    if using not in (None, "default"):
        raise ValueError("Only the default database is supported.")
    current = _current.get()
    nested = current is not None and current[0] is asyncio.current_task()
    if durable and nested:
        raise RuntimeError("A durable atomic block cannot be nested.")
    async with _session() as session:
        if nested and savepoint:
            async with _savepoint(session):
                yield
        else:
            yield
