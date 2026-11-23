"""Async session ownership scoped to the current task."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from obox.db.engine import _factory

_current: ContextVar[tuple[asyncio.Task[Any] | None, AsyncSession] | None] = ContextVar(
    "obox.session", default=None
)


@asynccontextmanager
async def bind_session(session: AsyncSession) -> AsyncIterator[None]:
    token = _current.set((asyncio.current_task(), session))
    try:
        yield
    finally:
        _current.reset(token)


@asynccontextmanager
async def _session() -> AsyncIterator[AsyncSession]:
    current = _current.get()
    if current is not None and current[0] is asyncio.current_task():
        yield current[1]
        return
    async with _factory()() as session, session.begin(), bind_session(session):
        yield session
