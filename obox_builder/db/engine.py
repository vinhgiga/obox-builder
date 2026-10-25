from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any, TypeVar

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Mapper
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.sql import visitors
from sqlalchemy.sql.dml import Delete, Insert, Update
from sqlalchemy.sql.elements import ClauseElement

from obox_builder.apps import apps
from obox_builder.conf import settings
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.db.router import router

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
    durable_transactions = bool(
        apps.models_ready
        and (
            (settings.EVENTS_ENABLED and apps.is_installed("obox_builder.contrib.events"))
            or (settings.JOBS_ENABLED and apps.is_installed("obox_builder.contrib.jobs"))
            or apps.is_installed("obox_builder.contrib.imports")
        )
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragma(dbapi_connection: Any, _record: Any) -> None:
        # Disable sqlite3's legacy implicit transaction handling. In legacy
        # mode a SAVEPOINT before the first DML lives outside the outer
        # transaction and can survive its rollback.
        if durable_transactions:
            dbapi_connection.isolation_level = None
        from obox_builder.db.collation import sqlite_sort_key
        from obox_builder.db.search import fold_text

        dbapi_connection.create_function("obox_search_text", 1, fold_text, deterministic=True)
        dbapi_connection.create_function("obox_vi_sort_key", 1, sqlite_sort_key())
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def _begin(connection: Any) -> None:
        immediate = connection.get_execution_options().get("sqlite_begin_immediate", False)
        if durable_transactions or immediate:
            connection.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")


def _check_postgres_collation(engine: AsyncEngine) -> None:
    if engine.dialect.name != "postgresql" or settings.ADMIN_TEXT_COLLATION != "vi":
        return

    @event.listens_for(engine.sync_engine, "connect")
    def _check_collation(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(
                "SELECT collprovider::text, collisdeterministic FROM pg_catalog.pg_collation "
                "WHERE collnamespace = 'pg_catalog'::regnamespace AND collname = 'vi-x-icu'"
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
            dbapi_connection.rollback()
        if row is None or row[0] != "i" or not row[1]:
            raise ImproperlyConfigured(
                'ADMIN_TEXT_COLLATION="vi" requires PostgreSQL with the deterministic '
                'ICU collation pg_catalog."vi-x-icu". Use a PostgreSQL build with ICU support.'
            )


class _EngineState:
    def __init__(self) -> None:
        self._engines: dict[str, AsyncEngine] = {}
        self._sessionmakers: dict[str | None, async_sessionmaker[AsyncSession]] = {}

    def engine(self, using: str = "default") -> AsyncEngine:
        if using not in settings.database_urls:
            configured = ", ".join(sorted(settings.database_urls))
            raise ImproperlyConfigured(
                f"Unknown database alias {using!r}. Configured aliases: {configured}."
            )
        if using not in self._engines:
            engine = create_async_engine(settings.database_urls[using], pool_pre_ping=True)
            _enable_sqlite_foreign_keys(engine)
            _check_postgres_collation(engine)
            self._engines[using] = engine
        return self._engines[using]

    def sessionmaker(self, using: str | None = None) -> async_sessionmaker[AsyncSession]:
        if using is not None and using not in settings.database_urls:
            self.engine(using)  # Raises the standard unknown-alias error.
        if using not in self._sessionmakers:
            options: dict[str, Any] = {
                "sync_session_class": RoutingSession,
                "expire_on_commit": False,
            }
            if using is not None:
                options["bind"] = self.engine(using)
                options["info"] = {"using": using}
            self._sessionmakers[using] = async_sessionmaker(**options)
        return self._sessionmakers[using]

    async def dispose(self) -> None:
        engines = list(self._engines.values())
        self._engines.clear()
        self._sessionmakers.clear()
        await asyncio.gather(*(engine.dispose() for engine in engines))

    def reset(self) -> None:
        """Drop cached engine/sessionmaker without disposing (tests)."""
        self._engines.clear()
        self._sessionmakers.clear()


def _mapper_for_clause(clause: ClauseElement | None) -> Mapper[Any] | None:
    table = getattr(clause, "table", None)
    if table is None:
        return None
    from obox_builder.db.base import ModelBase

    return next(
        (mapper for mapper in ModelBase.registry.mappers if mapper.local_table is table), None
    )


def _statement_mappers(state: Any) -> set[Mapper[Any]]:
    mappers = set(state.all_mappers)
    for element in visitors.iterate(state.statement):
        mapper = getattr(element, "_annotations", {}).get("parentmapper")
        if isinstance(mapper, Mapper):
            mappers.add(mapper)
    return mappers


class RoutingSession(SyncSession):
    """Synchronous facade used by AsyncSession to route ORM work by model."""

    def get_bind(
        self,
        mapper: type[Any] | Mapper[Any] | None = None,
        *,
        clause: ClauseElement | None = None,
        bind: sa.engine.Engine | sa.engine.Connection | None = None,
        _sa_skip_events: bool | None = None,
        _sa_skip_for_implicit_returning: bool = False,
        **kw: Any,
    ) -> sa.engine.Engine | sa.engine.Connection:
        using = self.info.get("using")
        if using is not None:
            return _state.engine(str(using)).sync_engine

        if isinstance(mapper, type):
            mapper = sa.inspect(mapper)
        mapper = mapper or _mapper_for_clause(clause)
        if mapper is not None:
            is_write = self._flushing or isinstance(clause, (Insert, Update, Delete))
            model = mapper.class_
            alias = router.db_for_write(model) if is_write else router.db_for_read(model)
            return _state.engine(alias).sync_engine
        if bind is not None:
            return bind
        return _state.engine().sync_engine


@event.listens_for(RoutingSession, "do_orm_execute")
def _validate_statement_routes(state: Any) -> None:
    using = state.session.info.get("using")
    mappers = _statement_mappers(state)
    if using is not None or not mappers:
        return
    is_write = state.is_insert or state.is_update or state.is_delete
    aliases = {
        router.db_for_write(mapper.class_) if is_write else router.db_for_read(mapper.class_)
        for mapper in mappers
    }
    if len(aliases) > 1:
        raise InvalidRequestError(
            "An ORM statement cannot span multiple database aliases; use separate sessions."
        )


@event.listens_for(RoutingSession, "loaded_as_persistent")
def _remember_instance_database(session: RoutingSession, instance: object) -> None:
    state = sa.inspect(instance)
    if state is None:
        return
    alias = session.info.get("using") or router.db_for_read(type(instance), instance=instance)
    state.info["database_alias"] = alias


@event.listens_for(RoutingSession, "before_flush")
def _validate_relation_routes(
    session: RoutingSession, _flush_context: Any, _instances: Any
) -> None:
    for instance in session.new.union(session.dirty):
        state = sa.inspect(instance)
        model = type(instance)
        alias = session.info.get("using") or router.db_for_write(model, instance=instance)
        state.info.setdefault("database_alias", alias)
        for relation in state.mapper.relationships:
            if relation.key not in state.dict:
                continue
            value = state.dict[relation.key]
            if value is None:
                continue
            related = value if relation.uselist else (value,)
            for target in related:
                target_state = sa.inspect(target)
                target_alias = (
                    session.info.get("using")
                    or target_state.info.get("database_alias")
                    or router.db_for_write(type(target), instance=target)
                )
                target_state.info.setdefault("database_alias", target_alias)
                if not router.allow_relation(instance, target):
                    raise InvalidRequestError(
                        f"Cannot relate {model.__name__} on database {alias!r} to "
                        f"{type(target).__name__} on database {target_alias!r}."
                    )


_state = _EngineState()


def get_engine(using: str = "default") -> AsyncEngine:
    return _state.engine(using)


def get_sessionmaker(using: str | None = None) -> async_sessionmaker[AsyncSession]:
    """Return a routed sessionmaker, or pin all operations to the given alias."""
    return _state.sessionmaker(using)


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
