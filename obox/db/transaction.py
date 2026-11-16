"""Asynchronous transactions with Django's atomic context-manager contract."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from obox.db import _current, _session


@asynccontextmanager
async def atomic(
    using: str | None = None, savepoint: bool = True, durable: bool = False
) -> AsyncIterator[None]:
    if using not in (None, "default"):
        raise ValueError("Only the default database is supported.")
    current = _current.get()
    nested = current is not None and current[0] is asyncio.current_task()
    if durable and nested:
        raise RuntimeError("A durable atomic block cannot be nested.")
    async with _session() as session:
        if nested and savepoint:
            async with session.begin_nested():
                yield
        else:
            yield
