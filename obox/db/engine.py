"""Database engines and session factories for applications and standalone commands."""

from __future__ import annotations

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from obox.conf import settings
from obox.core.exceptions import ImproperlyConfigured

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


def default_session_factory() -> async_sessionmaker[AsyncSession]:
    url = _url()
    if url not in _factories:
        options = settings.DATABASES["default"].get("OPTIONS", {})
        engine = create_async_engine(url, connect_args=options)
        _factories[url] = async_sessionmaker(engine, expire_on_commit=False)
    return _factories[url]


async def close_engines() -> None:
    for factory in _factories.values():
        await factory.kw["bind"].dispose()
    _factories.clear()
