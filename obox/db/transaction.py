"""Asynchronous transactions with Django's atomic context-manager contract."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from obox.db.session import current_session, session_scope


@asynccontextmanager
async def savepoint_scope(session: AsyncSession) -> AsyncIterator[None]:
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
async def atomic() -> AsyncIterator[None]:
    current = current_session.get()
    nested = current is not None and current[0] is asyncio.current_task()
    async with session_scope() as session:
        if nested:
            async with savepoint_scope(session):
                yield
        else:
            yield
