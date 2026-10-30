"""Collect declared permissions, synchronize grants and resolve actors."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.apps import apps
from obox_builder.contrib.auth.base_user import AbstractBaseUser
from obox_builder.contrib.auth.models import Permission, Role, role_permissions, user_roles
from obox_builder.exceptions import ImproperlyConfigured
from obox_builder.security import Actor, PermissionRef
from obox_builder.utils.strings import camel_to_snake

logger = logging.getLogger(__name__)


def model_identifier(model: type[Any]) -> str:
    return camel_to_snake(model.__name__)


@dataclass(frozen=True)
class AuthorizationCatalog:
    permissions: Mapping[str, PermissionRef]
    roles: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "permissions", MappingProxyType(dict(self.permissions)))
        roles = {name: frozenset(grants) for name, grants in self.roles.items()}
        for name, grants in roles.items():
            if not name.strip() or len(name) > 150:
                raise ImproperlyConfigured("Role names must contain 1 to 150 characters.")
            unknown = grants - self.permissions.keys()
            if unknown:
                raise ImproperlyConfigured(f"Unknown permissions on {name!r}: {sorted(unknown)}.")
        object.__setattr__(self, "roles", MappingProxyType(roles))


_catalog: AuthorizationCatalog | None = None


def initialize_catalog() -> None:
    global _catalog
    from obox_builder.contrib.auth.roles import discover_roles
    from obox_builder.resources import get_registry

    _catalog = AuthorizationCatalog(
        get_registry().permissions,
        discover_roles(app.name for app in apps.get_app_configs()),
    )


def collect_catalog() -> AuthorizationCatalog:
    if _catalog is None:
        raise ImproperlyConfigured("Authorization catalog is not ready; call obox_builder.setup().")
    return _catalog


def collect_permissions() -> list[PermissionRef]:
    return list(collect_catalog().permissions.values())


def validate_permission_references(names: Iterable[str]) -> None:
    unknown = set(names) - collect_catalog().permissions.keys()
    if unknown:
        raise ImproperlyConfigured(f"Unknown permissions: {sorted(unknown)}.")


@dataclass
class SyncResult:
    created: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    unchanged: int = 0


async def sync_permissions(
    db: AsyncSession,
    *,
    prune: bool = False,
    dry_run: bool = False,
    catalog: AuthorizationCatalog | None = None,
) -> SyncResult:
    """Sync permissions and create missing role templates, preserving existing grants."""
    catalog = collect_catalog() if catalog is None else catalog
    wanted = catalog.permissions
    existing = {row.name: row for row in await db.scalars(select(Permission))}
    result = SyncResult()
    result.retired = sorted(existing.keys() - wanted.keys())
    for name, entry in wanted.items():
        if name in existing:
            result.unchanged += 1
        else:
            result.created.append(name)
            if not dry_run:
                row = Permission(
                    service=entry.service, resource=entry.resource, action=entry.action
                )
                db.add(row)
                existing[name] = row
    if prune:
        for name, row in existing.items():
            if name not in wanted:
                result.pruned.append(name)
                if not dry_run:
                    await db.execute(
                        delete(role_permissions).where(role_permissions.c.permission_id == row.id)
                    )
                    await db.delete(row)
    if not dry_run:
        for name, grants in catalog.roles.items():
            if await db.scalar(select(Role.id).where(Role.name == name)) is None:
                db.add(Role(name=name, permissions=[existing[grant] for grant in sorted(grants)]))
    if not dry_run:
        await db.flush()
    return result


async def get_user_permissions(db: AsyncSession, user_id: int) -> frozenset[str]:
    return (await get_users_permissions(db, [user_id]))[user_id]


async def get_users_permissions(
    db: AsyncSession, user_ids: Iterable[int]
) -> dict[int, frozenset[str]]:
    """Stored role grants in one query; callers handle active status and superusers."""
    ids = set(user_ids)
    grouped: dict[int, set[str]] = {user_id: set() for user_id in ids}
    if ids:
        statement = (
            select(user_roles.c.user_id, Permission.service, Permission.resource, Permission.action)
            .join(role_permissions, Permission.id == role_permissions.c.permission_id)
            .join(user_roles, role_permissions.c.role_id == user_roles.c.role_id)
            .where(user_roles.c.user_id.in_(ids))
        )
        for user_id, service, resource, action in (await db.execute(statement)).all():
            grouped[user_id].add(f"{service}.{resource}.{action}")
    return {user_id: frozenset(values) for user_id, values in grouped.items()}


async def get_actor(
    db: AsyncSession, user: AbstractBaseUser, *, catalog: Mapping[str, Any] | None = None
) -> Actor:
    """Resolve a fresh snapshot of role names and effective permission names."""
    roles: set[str] = set()
    permissions: set[str] = set()
    if user.is_active:
        declared = (collect_catalog().permissions if catalog is None else catalog).keys()
        statement = (
            select(Role.name, Permission.service, Permission.resource, Permission.action)
            .join(user_roles, user_roles.c.role_id == Role.id)
            .outerjoin(role_permissions, role_permissions.c.role_id == Role.id)
            .outerjoin(Permission, Permission.id == role_permissions.c.permission_id)
            .where(user_roles.c.user_id == user.id)
        )
        for role, service, resource, action in (await db.execute(statement)).all():
            roles.add(role)
            if service is not None:
                permissions.add(f"{service}.{resource}.{action}")
        if user.is_superuser:
            permissions = set(declared)
        else:
            permissions.intersection_update(declared)
    return Actor(
        id=user.id,
        roles=frozenset(roles),
        permissions=frozenset(permissions),
        is_active=user.is_active,
        is_superuser=user.is_superuser,
        attributes={"username": user.get_username()},
    )


async def sync_permissions_receiver(sender: object, **kwargs: object) -> None:
    from obox_builder.db.router import router
    from obox_builder.db.session import session_scope

    database = str(kwargs.get("database", "default"))
    if not router.allow_migrate_model(database, Permission):
        return
    async with session_scope(database) as db:
        result = await sync_permissions(db)
    if result.created:
        logger.info("Created %d permission(s): %s", len(result.created), ", ".join(result.created))
    else:
        logger.info("Permissions are in sync (%d existing).", result.unchanged)
