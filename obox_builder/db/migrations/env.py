"""Alembic environment used by every project's ``migrations/env.py``."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from obox_builder.conf import settings
from obox_builder.db.base import ModelBase
from obox_builder.db.engine import to_sync_url
from obox_builder.db.migrations.config import make_include_object, obox_render_item


def run_migrations() -> None:
    """Run Alembic migrations; called from the project's ``migrations/env.py``."""
    import obox_builder

    obox_builder.setup()
    config = context.config
    url = config.attributes.get("connection_url") or to_sync_url(settings.DATABASE_URL)
    target_metadata = ModelBase.metadata
    include_object = make_include_object()

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
            context.run_migrations()
        return

    connectable = create_engine(url, poolclass=pool.NullPool)
    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                include_object=include_object,
                render_item=obox_render_item,
                render_as_batch=connection.dialect.name == "sqlite",
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()
