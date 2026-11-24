"""Async session ownership scoped to the current task."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from obox.db.engine import default_session_factory

current_session: ContextVar[tuple[asyncio.Task[Any] | None, AsyncSession] | None] = ContextVar(
    "obox.session", default=None
)


@asynccontextmanager
async def bind_session(session: AsyncSession) -> AsyncIterator[None]:
    token = current_session.set((asyncio.current_task(), session))
    try:
        yield
    finally:
        current_session.reset(token)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    current = current_session.get()
    if current is not None and current[0] is asyncio.current_task():
        yield current[1]
        return
    async with default_session_factory()() as session, session.begin(), bind_session(session):
        yield session
