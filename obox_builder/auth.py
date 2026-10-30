"""Fail-closed authorization; identity resolution belongs to the host application."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

import sqlalchemy as sa
from sqlalchemy.sql.elements import ColumnElement

from obox_builder.context import Context

T = TypeVar("T")


@dataclass(frozen=True)
class Principal:
    id: Any = None
    is_authenticated: bool = False
    is_staff: bool = False
    is_active: bool = True


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
