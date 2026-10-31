"""Fail-closed authorization; identity resolution belongs to the host application."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from inspect import iscoroutinefunction
from typing import Any, Generic, Protocol, TypeVar

import sqlalchemy as sa
from sqlalchemy.sql.elements import ColumnElement

from obox_builder.context import Context
from obox_builder.errors import ImproperlyConfigured

T = TypeVar("T")


def _valid_permission(name: str) -> bool:
    parts = name.split(".")
    return len(parts) == 3 and all(part.isidentifier() for part in parts)


class Identity(Protocol):
    """Minimal identity returned by authentication; has_perm is synchronous."""

    @property
    def id(self) -> Any: ...

    @property
    def username(self) -> str: ...

    @property
    def is_authenticated(self) -> bool: ...

    @property
    def is_active(self) -> bool: ...

    def has_perm(self, permission: str) -> bool: ...


@dataclass(frozen=True)
class Principal:
    id: Any = None
    is_authenticated: bool = False
    is_staff: bool = False
    is_active: bool = True
    username: str = ""
    is_superuser: bool = False
    roles: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()

    def has_perm(self, permission: str) -> bool:
        return (
            self.is_authenticated
            and self.is_active
            and _valid_permission(permission)
            and (self.is_superuser or permission in self.permissions)
        )


class Policy(Generic[T]):
    async def allow(self, action: str, ctx: Context, *, obj: T | None = None) -> bool:
        return False

    def scope(self, action: str, ctx: Context) -> ColumnElement[bool]:
        return sa.false()

    def read_fields(self, ctx: Context, *, obj: T | None = None) -> frozenset[str] | None:
        return None

    def write_fields(
        self, action: str, ctx: Context, *, obj: T | None = None
    ) -> frozenset[str] | None:
        return None

    def presets(self, action: str, ctx: Context) -> Mapping[str, Any]:
        return {}

    async def check(self, action: str, instance: T, ctx: Context) -> bool:
        return False


class StaffOnly(Policy[T]):
    async def allow(self, action: str, ctx: Context, *, obj: T | None = None) -> bool:
        actor = ctx.principal
        return bool(
            getattr(actor, "is_authenticated", False) is True
            and getattr(actor, "is_active", True) is True
            and getattr(actor, "is_staff", False) is True
        )

    def scope(self, action: str, ctx: Context) -> ColumnElement[bool]:
        return sa.true()

    async def check(self, action: str, instance: T, ctx: Context) -> bool:
        return await self.allow(action, ctx, obj=instance)


class PublicReadOnly(Policy[T]):
    async def allow(self, action: str, ctx: Context, *, obj: T | None = None) -> bool:
        return action in {"list", "retrieve"}

    def scope(self, action: str, ctx: Context) -> ColumnElement[bool]:
        return sa.true() if action in {"list", "retrieve"} else sa.false()


class RBAC(Policy[T]):
    """Exact service.resource.action grants, e.g. RBAC('blog.questions')."""

    def __init__(self, resource: str) -> None:
        if not _valid_permission(f"{resource}.list"):
            raise ImproperlyConfigured("RBAC requires service.resource, e.g. blog.questions.")
        self.resource = resource

    async def allow(self, action: str, ctx: Context, *, obj: T | None = None) -> bool:
        return self._allowed(action, ctx)

    def _allowed(self, action: str, ctx: Context) -> bool:
        actor = ctx.principal
        has_perm = getattr(actor, "has_perm", None)
        return bool(
            getattr(actor, "is_authenticated", False) is True
            and getattr(actor, "is_active", False) is True
            and action in {"list", "retrieve", "create", "update", "delete"}
            and callable(has_perm)
            and not iscoroutinefunction(has_perm)
            and has_perm(f"{self.resource}.{action}") is True
        )

    def scope(self, action: str, ctx: Context) -> ColumnElement[bool]:
        return sa.true() if self._allowed(action, ctx) else sa.false()

    async def check(self, action: str, instance: T, ctx: Context) -> bool:
        return self._allowed(action, ctx)
