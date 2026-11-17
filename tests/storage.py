"""Isolated SQLite files or PostgreSQL schemas for feature integration tests."""

import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@asynccontextmanager
async def database(
    path: Path, metadata: Sequence[sa.MetaData]
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(os.getenv("OBOX_TEST_DATABASE_URL", f"sqlite+aiosqlite:///{path}"))
    namespace = "feature_" + uuid4().hex if engine.dialect.name == "postgresql" else None
    if namespace:
        async with engine.begin() as connection:
            await connection.execute(sa.schema.CreateSchema(namespace))
        engine = engine.execution_options(schema_translate_map={None: namespace})
    try:
        async with engine.begin() as connection:
            for item in metadata:
                await connection.run_sync(item.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        if namespace:
            async with engine.begin() as connection:
                await connection.execute(sa.schema.DropSchema(namespace, cascade=True))
        await engine.dispose()
