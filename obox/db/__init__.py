"""Native SQLAlchemy sessions, scoped to one asynchronous task."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from obox.conf import settings
from obox.core.exceptions import ImproperlyConfigured

_current: ContextVar[tuple[Any, AsyncSession] | None] = ContextVar("obox.session", default=None)
_factories: dict[str, async_sessionmaker[AsyncSession]] = {}


def _url() -> str:
    config = settings.DATABASES.get("default")
    if not config:
        raise ImproperlyConfigured("DATABASES must define a default database.")
    engine = config.get("ENGINE", "obox.db.backends.sqlite3")
    if engine == "obox.db.backends.sqlite3":
        return "sqlite+aiosqlite:///" + str(config.get("NAME", ":memory:"))
    if engine == "obox.db.backends.postgresql":
        return URL.create(
            "postgresql+asyncpg",
            username=config.get("USER"),
            password=config.get("PASSWORD"),
            host=config.get("HOST") or None,
            port=int(config["PORT"]) if config.get("PORT") else None,
            database=config.get("NAME"),
        ).render_as_string(hide_password=False)
    raise ImproperlyConfigured(f"Unsupported database backend: {engine!r}.")


def _factory() -> async_sessionmaker[AsyncSession]:
    url = _url()
    if url not in _factories:
        options = settings.DATABASES["default"].get("OPTIONS", {})
        engine = create_async_engine(url, connect_args=options)
        _factories[url] = async_sessionmaker(engine, expire_on_commit=False)
    return _factories[url]


@asynccontextmanager
async def _session() -> AsyncIterator[AsyncSession]:
    current = _current.get()
    if current is not None and current[0] is asyncio.current_task():
        yield current[1]
        return
    async with _factory()() as session, session.begin():
        token = _current.set((asyncio.current_task(), session))
        try:
            yield session
        finally:
            _current.reset(token)


async def _close() -> None:
    for factory in _factories.values():
        await factory.kw["bind"].dispose()
    _factories.clear()
