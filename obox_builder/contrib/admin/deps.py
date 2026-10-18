"""Router dependencies shared by admin routes."""

from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.apps import apps
from obox_builder.db.session import get_db

HISTORY_APP = "obox_builder.contrib.history"


async def bind_history(request: Request, db: AsyncSession = Depends(get_db)) -> None:
    """Bind the request's actor context to the session used by history capture.

    Runs before authentication on purpose: the resolver is late-bound, so it
    reads ``request.state.user`` when the flush happens, not now.
    """
    if apps.is_installed(HISTORY_APP):
        from obox_builder.contrib.history import bind

        bind(db, request)
