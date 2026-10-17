from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any, TypeVar

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from obox_builder.conf import settings
from obox_builder.core.exceptions import ImproperlyConfigured

T = TypeVar("T")

SYNC_DRIVERS = {
    "postgresql+psycopg_async": "postgresql+psycopg",
    "postgresql+asyncpg": "postgresql+psycopg",
    "sqlite+aiosqlite": "sqlite",
}


def to_sync_url(url: str) -> str:
    """Translate an async SQLAlchemy URL to its synchronous driver (Alembic/CLI)."""
    parsed = sa.engine.make_url(url)
    sync_driver = SYNC_DRIVERS.get(parsed.drivername)
    if sync_driver is None:
        if parsed.drivername in {"postgresql+psycopg", "sqlite"}:
            return url
        raise ImproperlyConfigured(
            f"Unsupported DATABASE_URL driver {parsed.drivername!r}. Supported: "
            "postgresql+psycopg_async, postgresql+asyncpg, sqlite+aiosqlite."
        )
    return parsed.set(drivername=sync_driver).render_as_string(hide_password=False)


def _enable_sqlite_foreign_keys(engine: AsyncEngine) -> None:
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragma(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


class _EngineState:
    def __init__(self) -> None:
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None

    def engine(self) -> AsyncEngine:
        if self._engine is None:
            url = settings.DATABASE_URL
            self._engine = create_async_engine(url, pool_pre_ping=True)
            _enable_sqlite_foreign_keys(self._engine)
        return self._engine

    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        if self._sessionmaker is None:
            self._sessionmaker = async_sessionmaker(self.engine(), expire_on_commit=False)
        return self._sessionmaker

    async def dispose(self) -> None:
        engine = self._engine
        self._engine = None
        self._sessionmaker = None
        if engine is not None:
            await engine.dispose()

    def reset(self) -> None:
        """Drop cached engine/sessionmaker without disposing (tests)."""
        self._engine = None
        self._sessionmaker = None


_state = _EngineState()


def get_engine() -> AsyncEngine:
    return _state.engine()


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return _state.sessionmaker()


async def dispose() -> None:
    await _state.dispose()


def _reset() -> None:
    _state.reset()


async def _run_with_dispose(awaitable: Awaitable[Any]) -> Any:
    try:
        return await awaitable
    finally:
        await dispose()


def run_async(awaitable: Awaitable[T]) -> T:
    """Run a coroutine (typically a management command) and dispose engines after."""
    return asyncio.run(_run_with_dispose(awaitable))
