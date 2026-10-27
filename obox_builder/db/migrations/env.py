"""Alembic environment used by every project's ``migrations/env.py``."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import create_async_engine

from obox_builder.conf import settings
from obox_builder.db.base import ModelBase
from obox_builder.db.migrations.config import database_key, make_include_object, obox_render_item


def do_run_migrations(connection: Connection, alias: str, engine_name: str) -> None:
    context.configure(
        connection=connection,
        target_metadata=ModelBase.metadata,
        compare_type=True,
        include_object=make_include_object(alias),
        render_item=obox_render_item,
        render_as_batch=connection.dialect.name == "sqlite",
        upgrade_token=f"{engine_name}_upgrades",
        downgrade_token=f"{engine_name}_downgrades",
    )
    with context.begin_transaction():
        context.run_migrations(engine_name=engine_name)


async def run_async_migrations(url: str, alias: str, engine_name: str) -> None:
    connectable = create_async_engine(url, poolclass=pool.NullPool)
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations, alias, engine_name)
    finally:
        await connectable.dispose()


def run_migrations() -> None:
    """Run Alembic migrations; called from the project's ``migrations/env.py``."""
    import obox_builder

    obox_builder.setup()
    config = context.config
    database = config.attributes.get("database_alias", "default")
    aliases = config.attributes.get("database_aliases", tuple(settings.database_urls))
    target_metadata = ModelBase.metadata
    autogenerate_multi_db = config.attributes.get("autogenerate_multi_db", False)
    databases = aliases if autogenerate_multi_db else (database,)

    for alias in databases:
        engine_name = database_key(alias)
        include_object = make_include_object(alias)
        url = (config.attributes.get("connection_url") if alias == database else None) or (
            settings.database_urls[alias]
        )

        if context.is_offline_mode():
            context.configure(
                url=url,
                target_metadata=target_metadata,
                literal_binds=True,
                compare_type=True,
                include_object=include_object,
                render_item=obox_render_item,
                dialect_opts={"paramstyle": "named"},
            )
            with context.begin_transaction():
                context.run_migrations(engine_name=engine_name)
            continue

        asyncio.run(run_async_migrations(url, alias, engine_name))
