"""Permission naming, collection from the app registry, and sync."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.apps import apps
from obox_builder.contrib.auth.models import Permission, role_permissions, user_roles
from obox_builder.db.base import camel_to_snake

logger = logging.getLogger(__name__)

DEFAULT_ACTIONS = ("view", "add", "change", "delete")


def model_identifier(model: type[Any]) -> str:
    return camel_to_snake(model.__name__)


def permission_codename(action: str, model_name: str) -> str:
    return f"{action}_{model_name}"


def permission_label(action: str, model_name: str) -> str:
    return f"Can {action} {model_name.replace('_', ' ')}"


@dataclass
class PermissionEntry:
    app_label: str
    codename: str
    name: str
    model: str | None

    @property
    def full_name(self) -> str:
        return f"{self.app_label}.{self.codename}"


def collect_permissions() -> list[PermissionEntry]:
    """Permissions wanted by the registry: model defaults, model extras, app extras."""
    entries: list[PermissionEntry] = []
    seen: set[tuple[str, str]] = set()

    def add(entry: PermissionEntry) -> None:
        key = (entry.app_label, entry.codename)
        if key not in seen:
            seen.add(key)
            entries.append(entry)

    for model in apps.get_models():
        app_label = model._model_label
        model_name = model_identifier(model)
        for action in DEFAULT_ACTIONS:
            add(
                PermissionEntry(
                    app_label=app_label,
                    codename=permission_codename(action, model_name),
                    name=permission_label(action, model_name),
                    model=model_name,
                )
            )
        for action, name in getattr(model, "__permissions__", []):
            add(
                PermissionEntry(
                    app_label=app_label,
                    codename=permission_codename(action, model_name),
                    name=name,
                    model=model_name,
                )
            )
    for app_config in apps.get_app_configs():
        for codename, name in app_config.permissions:
            add(
                PermissionEntry(
                    app_label=app_config.label,
                    codename=codename,
                    name=name,
                    model=None,
                )
            )
    return entries


@dataclass
class SyncResult:
    created: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    unchanged: int = 0


async def sync_permissions(
    db: AsyncSession, *, prune: bool = False, dry_run: bool = False
) -> SyncResult:
    """Create permissions missing from the database; idempotent."""
    wanted = {(entry.app_label, entry.codename): entry for entry in collect_permissions()}
    existing = {(row.app_label, row.codename): row for row in await db.scalars(select(Permission))}
    result = SyncResult()
    for key, entry in wanted.items():
        row = existing.get(key)
        if row is None:
            result.created.append(entry.full_name)
            if not dry_run:
                db.add(
                    Permission(
                        app_label=entry.app_label,
                        codename=entry.codename,
                        name=entry.name,
                        model=entry.model,
                    )
                )
        else:
            result.unchanged += 1
            if not dry_run and (row.name != entry.name or row.model != entry.model):
                row.name = entry.name
                row.model = entry.model
    if prune:
        for key, row in existing.items():
            if key not in wanted:
                result.pruned.append(f"{row.app_label}.{row.codename}")
                if not dry_run:
                    await db.execute(
                        delete(role_permissions).where(role_permissions.c.permission_id == row.id)
                    )
                    await db.delete(row)
    await db.flush()
    return result


async def get_user_permissions(db: AsyncSession, user_id: int) -> frozenset[str]:
    """Effective permission strings (``app_label.codename``) of a user."""
    statement = (
        select(Permission.app_label, Permission.codename)
        .join(role_permissions, Permission.id == role_permissions.c.permission_id)
        .join(user_roles, role_permissions.c.role_id == user_roles.c.role_id)
        .where(user_roles.c.user_id == user_id)
    )
    rows = (await db.execute(statement)).all()
    return frozenset(f"{app_label}.{codename}" for app_label, codename in rows)


async def sync_permissions_receiver(sender: object, **kwargs: object) -> None:
    """``post_migrate`` receiver: keep permissions in sync after migrations."""
    from obox_builder.db.session import session_scope

    async with session_scope() as db:
        result = await sync_permissions(db)
    if result.created:
        logger.info("Created %d permission(s): %s", len(result.created), ", ".join(result.created))
    else:
        logger.info("Permissions are in sync (%d existing).", result.unchanged)
