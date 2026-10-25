from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class PasswordBackend:
    async def authenticate(
        self, db: AsyncSession, *, request: Any = None, **credentials: Any
    ) -> Any:
        del request
        if credentials.get("password") != "backend-secret":
            return None
        from obox_builder.contrib.auth.models import User

        return await db.scalar(select(User).where(User.username == credentials.get("username")))

    async def get_user(self, db: AsyncSession, user_id: int) -> Any:
        from obox_builder.contrib.auth.models import User

        return await db.get(User, user_id)
