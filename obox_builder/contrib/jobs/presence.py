"""Durable worker presence, including idle workers and unclean exits."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obox_builder.contrib.jobs.models import Worker, utcnow

logger = logging.getLogger("obox.jobs")


async def _pulse(sessions: async_sessionmaker[AsyncSession], hostname: str) -> None:
    while True:
        await asyncio.sleep(10)
        try:
            async with sessions.begin() as db:
                await db.execute(
                    sa.update(Worker)
                    .where(Worker.hostname == hostname)
                    .values(heartbeat_at=utcnow())
                )
        except Exception as exc:
            logger.warning("Worker heartbeat failed: %s.", type(exc).__name__)


@asynccontextmanager
async def presence(
    sessions: async_sessionmaker[AsyncSession],
    hostname: str,
    queues: tuple[str, ...],
    concurrency: int,
) -> AsyncIterator[None]:
    now = utcnow()
    async with sessions.begin() as db:
        insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
        values = {
            "hostname": hostname,
            "pid": os.getpid(),
            "queues": list(queues),
            "concurrency": concurrency,
            "started_at": now,
            "heartbeat_at": now,
            "stopped_at": None,
        }
        await db.execute(
            insert(Worker)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["hostname"],
                set_={key: value for key, value in values.items() if key != "hostname"},
            )
        )
    heartbeat = asyncio.create_task(_pulse(sessions, hostname))
    try:
        yield
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
        try:
            async with sessions.begin() as db:
                await db.execute(
                    sa.update(Worker).where(Worker.hostname == hostname).values(stopped_at=utcnow())
                )
        except Exception as exc:
            logger.warning("Worker shutdown presence update failed: %s.", type(exc).__name__)
