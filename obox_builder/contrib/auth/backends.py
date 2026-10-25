"""Pluggable authentication backends and the default database-backed backend."""

from __future__ import annotations

from importlib import import_module
from typing import Any, Protocol

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.contrib.auth.base_user import AbstractUser
from obox_builder.core.exceptions import ImproperlyConfigured


class AuthenticationBackend(Protocol):
    async def authenticate(
        self,
        db: AsyncSession,
        *,
        request: Request | None = None,
        **credentials: Any,
    ) -> AbstractUser | None: ...

    async def get_user(self, db: AsyncSession, user_id: int) -> AbstractUser | None: ...


class ModelBackend:
    """Authenticate against the configured SQLAlchemy user model."""

    async def authenticate(
        self,
        db: AsyncSession,
        *,
        request: Request | None = None,
        **credentials: Any,
    ) -> AbstractUser | None:
        del request
        from obox_builder.contrib.auth.services import _authenticate_model

        user = await _authenticate_model(
            db, str(credentials.get("username", "")), str(credentials.get("password", ""))
        )
        return user if user is not None and user.is_active else None

    async def get_user(self, db: AsyncSession, user_id: int) -> AbstractUser | None:
        from obox_builder.contrib.auth.models import User

        user = await db.get(User, user_id)
        return user if user is not None and user.is_active else None


def load_backend(path: str) -> AuthenticationBackend:
    """Import and instantiate a configured dotted backend class."""
    module_path, separator, name = path.rpartition(".")
    if not separator:
        raise ImproperlyConfigured(f"Authentication backend {path!r} must be a dotted path.")
    try:
        backend_class = getattr(import_module(module_path), name)
        backend = backend_class()
    except (ImportError, AttributeError, TypeError) as exc:
        raise ImproperlyConfigured(f"Cannot load authentication backend {path!r}: {exc}") from exc
    if not callable(getattr(backend, "authenticate", None)) or not callable(
        getattr(backend, "get_user", None)
    ):
        raise ImproperlyConfigured(
            f"Authentication backend {path!r} must define authenticate() and get_user()."
        )
    return backend
