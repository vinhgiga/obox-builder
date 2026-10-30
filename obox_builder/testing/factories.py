"""Test factories: build users, roles and permission assignments quickly.

All obox-builder imports are deliberately lazy: this module is imported by the
pytest plugin at startup, before a settings module exists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.orm import selectinload

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from obox_builder.contrib.auth.models import Permission, Role, User

DEFAULT_PASSWORD = "s3cure-password"  # noqa: S105 - test default, not a secret


async def make_user(
    username: str,
    *,
    password: str = DEFAULT_PASSWORD,
    email: str = "",
    full_name: str = "",
    is_superuser: bool = False,
    is_active: bool = True,
) -> User:
    """Create (or update) a user and return it.

    Reusing an existing username resets the password, so repeated fixture
    usage stays idempotent across a test.
    """
    from obox_builder.contrib.auth import services
    from obox_builder.contrib.auth.models import User
    from obox_builder.db.session import session_scope

    async with session_scope() as db:
        user = await db.scalar(
            sa.select(User).where(sa.func.lower(User.username) == username.strip().lower())
        )
        if user is None:
            user = await services.create_user(
                db, username, password, email=email, full_name=full_name
            )
        else:
            await services.set_password(db, user, password)
        user.is_superuser = is_superuser
        user.is_active = is_active
        await db.flush()
        return user


async def _resolve_permissions(db: AsyncSession, full_names: tuple[str, ...]) -> list[Permission]:
    from obox_builder.contrib.auth.models import Permission
    from obox_builder.security import PermissionRef

    resolved: list[Permission] = []
    for full_name in full_names:
        name = PermissionRef.parse(full_name)
        permission = await db.scalar(
            sa.select(Permission).where(
                Permission.service == name.service,
                Permission.resource == name.resource,
                Permission.action == name.action,
            )
        )
        if permission is None:
            raise ValueError(
                f"Permission {full_name!r} does not exist; run sync_permissions() first."
            )
        resolved.append(permission)
    return resolved


async def make_role(name: str, *permissions: str) -> Role:
    """Create (or update) a role holding the given permissions.

    ``permissions`` are ``service.resource.action`` strings, e.g.
    ``"auth.users.read"`` or ``"blog.post.publish"``.
    """
    from obox_builder.contrib.auth.models import Role
    from obox_builder.db.session import session_scope

    async with session_scope() as db:
        role = await db.scalar(sa.select(Role).where(Role.name == name))
        if role is None:
            role = Role(name=name)
            db.add(role)
            await db.flush()
        await db.refresh(role, ["permissions"])
        role.permissions = await _resolve_permissions(db, permissions)
        await db.flush()
        return role


async def grant_permissions(user: User, *permissions: str) -> None:
    """Attach a personal ``<username>-role`` holding the given permissions."""
    from obox_builder.contrib.auth.models import Role, User
    from obox_builder.db.session import session_scope

    role_name = f"{user.username}-role"
    async with session_scope() as db:
        role = await db.scalar(sa.select(Role).where(Role.name == role_name))
        if role is None:
            role = Role(name=role_name)
            db.add(role)
            await db.flush()
        await db.refresh(role, ["permissions"])
        for permission in await _resolve_permissions(db, permissions):
            if permission not in role.permissions:
                role.permissions.append(permission)
        persisted = await db.scalar(
            sa.select(User).where(User.id == user.id).options(selectinload(User.roles))
        )
        if persisted is None:  # pragma: no cover - the user was deleted mid-test
            raise LookupError(f"User {user.id} no longer exists.")
        if role not in persisted.roles:
            persisted.roles.append(role)
        await db.flush()


__all__: list[str] = [
    "DEFAULT_PASSWORD",
    "grant_permissions",
    "make_role",
    "make_user",
]
