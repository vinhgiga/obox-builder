"""Durable transactional events (optional installed app)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.core.events import emit, on

__all__ = ["emit", "on", "publish"]


async def publish(
    db: AsyncSession, type: str, payload: dict[str, Any] | None = None, **options: Any
) -> uuid.UUID | None:
    """Stage an event; the caller owns flush/commit/rollback."""
    return emit(db, type, payload, **options)
