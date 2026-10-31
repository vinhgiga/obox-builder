"""Replace the backend entirely or override individual model-backed hooks."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import sqlalchemy as sa
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from obox_builder.auth import Identity, Principal
from obox_builder.contrib.auth.models import Session, User
from obox_builder.contrib.auth.passwords import hash_password, verify_password
from obox_builder.errors import ImproperlyConfigured


class Backend(Protocol):
    async def resolve(self, token: str, *, request: Request | None = None) -> Identity: ...

    async def login(
        self,
        username: str,
        password: str,
        *,
        previous: str = "",
        max_age: int = 86400,
        request: Request | None = None,
    ) -> str | None: ...

    async def logout(self, token: str, *, request: Request | None = None) -> None: ...


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ModelBackend:
    """Models use the supplied mixins and a single user primary key of any SQL type."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        user_model: type[Any] = User,
        session_model: type[Any] = Session,
        username_field: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.user_model, self.session_model = user_model, session_model
        user_pk = sa.inspect(user_model).primary_key
        session_user = sa.inspect(session_model).columns["user_id"]
        if len(user_pk) != 1 or not any(
            fk.column is user_pk[0] for fk in session_user.foreign_keys
        ):
            raise ImproperlyConfigured(
                "session_model.user_id must reference the user model's primary key."
            )
        self.username_field = (
            username_field
            if username_field is not None
            else str(getattr(user_model, "USERNAME_FIELD", "username"))
        )

    async def hash_password(self, password: str) -> str:
        return await hash_password(password)

    async def verify_password(self, password: str, encoded: str | None) -> bool:
        return await verify_password(password, encoded)

    def user_can_authenticate(self, user: Any) -> bool:
        return getattr(user, "is_active", False) is True

    def get_user_id(self, user: Any) -> Any:
        identity = sa.inspect(user).identity
        if identity is None or len(identity) != 1:
            raise ImproperlyConfigured(
                "ModelBackend requires a persisted user with a single primary key."
            )
        return identity[0]

    def session_auth_hash(self, user: Any) -> str:
        return hashlib.sha256(user.password_hash.encode()).hexdigest()

    async def get_user(
        self, db: AsyncSession, user_id: Any, *, request: Request | None = None
    ) -> Any:
        options = [selectinload(self.user_model.roles)] if hasattr(self.user_model, "roles") else []
        return await db.get(self.user_model, user_id, options=options)

    async def get_permissions(
        self, db: AsyncSession, user: Any, *, request: Request | None = None
    ) -> frozenset[str]:
        return frozenset(grant for role in getattr(user, "roles", ()) for grant in role.permissions)

    async def to_principal(
        self, db: AsyncSession, user: Any, *, request: Request | None = None
    ) -> Identity:
        return Principal(
            id=self.get_user_id(user),
            username=str(getattr(user, self.username_field)),
            is_authenticated=True,
            is_active=user.is_active,
            is_staff=getattr(user, "is_staff", False),
            is_superuser=getattr(user, "is_superuser", False),
            roles=frozenset(role.name for role in getattr(user, "roles", ())),
            permissions=await self.get_permissions(db, user, request=request),
        )

    async def authenticate(
        self, db: AsyncSession, username: str, password: str, *, request: Request | None = None
    ) -> Any:
        if not username or len(username) > 1024 or not password or len(password) > 1024:
            return None
        column = getattr(self.user_model, self.username_field)
        user = (
            await db.scalars(sa.select(self.user_model).where(column == username))
        ).one_or_none()
        valid = await self.verify_password(password, user.password_hash if user else None)
        return user if valid and user is not None and self.user_can_authenticate(user) else None

    async def create_session(
        self, db: AsyncSession, user: Any, *, max_age: int, request: Request | None = None
    ) -> str:
        token = secrets.token_urlsafe(32)
        db.add(
            self.session_model(
                token_hash=hash_token(token),
                user_id=self.get_user_id(user),
                auth_hash=self.session_auth_hash(user),
                expires_at=datetime.now(UTC) + timedelta(seconds=max_age),
            )
        )
        return token

    async def resolve(self, token: str, *, request: Request | None = None) -> Identity:
        if not token or len(token) > 128:
            return Principal()
        async with self.session_factory() as db:
            model = self.session_model
            session = await db.scalar(sa.select(model).where(model.token_hash == hash_token(token)))
            if session is None:
                return Principal()
            expiry = session.expires_at
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            if expiry <= datetime.now(UTC):
                return Principal()
            user = await self.get_user(db, session.user_id, request=request)
            if (
                user is None
                or not self.user_can_authenticate(user)
                or not hmac.compare_digest(session.auth_hash, self.session_auth_hash(user))
            ):
                return Principal()
            return await self.to_principal(db, user, request=request)

    async def login(
        self,
        username: str,
        password: str,
        *,
        previous: str = "",
        max_age: int = 86400,
        request: Request | None = None,
    ) -> str | None:
        async with self.session_factory.begin() as db:
            user = await self.authenticate(db, username, password, request=request)
            if user is None:
                return None
            model = self.session_model
            await db.execute(sa.delete(model).where(model.token_hash == hash_token(previous)))
            return await self.create_session(db, user, max_age=max_age, request=request)

    async def logout(self, token: str, *, request: Request | None = None) -> None:
        async with self.session_factory.begin() as db:
            model = self.session_model
            await db.execute(sa.delete(model).where(model.token_hash == hash_token(token)))
