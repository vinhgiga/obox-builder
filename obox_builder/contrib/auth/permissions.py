"""Collect declared permissions, synchronize grants and resolve actors."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.apps import apps
from obox_builder.contrib.auth.authz import (
    Actor,
    AuthorizationCatalog,
    PermissionName,
    authorization_catalog,
    model_permission,
)
from obox_builder.contrib.auth.base_user import AbstractBaseUser
from obox_builder.contrib.auth.models import Permission, Role, role_permissions, user_roles
from obox_builder.exceptions import ImproperlyConfigured
from obox_builder.utils.strings import camel_to_snake

logger = logging.getLogger(__name__)


def model_identifier(model: type[Any]) -> str:
    return camel_to_snake(model.__name__)


def collect_catalog() -> AuthorizationCatalog:
    """Combine model defaults, app declarations and route/service declarations."""
    catalog = AuthorizationCatalog()
    services: dict[str, str] = {}
    for config in apps.get_app_configs():
        service = config.label.replace("_", "")
        if service in services:
            raise ImproperlyConfigured(
                f"App labels {services[service]!r} and {config.label!r} share service {service!r}."
            )
        services[service] = config.label
        for name in config.permissions:
            permission = PermissionName.parse(name)
            catalog.register_permission(permission.service, permission.resource, permission.action)
    for model in apps.get_models():
        for action in (*model.__permission_actions__, *model.__permissions__):
            permission = PermissionName.parse(model_permission(model, action))
            catalog.register_permission(permission.service, permission.resource, permission.action)
    declared = authorization_catalog
    catalog.permissions.update(declared.permissions)
    catalog.roles.update(declared.roles)
    catalog.validate()
    return catalog


def collect_permissions() -> list[PermissionName]:
    return list(collect_catalog().permissions.values())


@dataclass
class SyncResult:
    created: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    unchanged: int = 0


async def sync_permissions(
    db: AsyncSession, *, prune: bool = False, dry_run: bool = False
) -> SyncResult:
    """Sync permissions and create missing role templates, preserving existing grants."""
    catalog = collect_catalog()
    wanted = catalog.permissions
    existing = {row.name: row for row in await db.scalars(select(Permission))}
    result = SyncResult()
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


async def get_actor(db: AsyncSession, user: AbstractBaseUser) -> Actor:
    """Resolve a fresh snapshot of role names and effective permission names."""
    roles: set[str] = set()
    permissions: set[str] = set()
    if user.is_active:
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
            permissions = {permission.name for permission in collect_permissions()}
    return Actor(
        id=user.id,
        roles=frozenset(roles),
        permissions=frozenset(permissions),
        is_active=user.is_active,
        is_superuser=user.is_superuser,
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
