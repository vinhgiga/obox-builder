from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.db.engine import get_sessionmaker


@asynccontextmanager
async def session_scope(using: str | None = None) -> AsyncIterator[AsyncSession]:
    """Commit each database transaction used on success; cross-database commits aren't atomic."""
    async with get_sessionmaker(using)() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI routed session; commits each used database on success, without cross-db atomicity."""
    async with get_sessionmaker()() as session:
        try:
            if session.get_bind().dialect.name == "sqlite":
                # HTTP views (including session renewal) commonly read before
                # writing. Reserve the writer up front when explicit SQLite
                # transactions are enabled, avoiding read-to-write deadlocks.
                await session.connection(execution_options={"sqlite_begin_immediate": True})
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
