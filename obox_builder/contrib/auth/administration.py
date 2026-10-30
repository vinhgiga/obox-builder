"""Shared delegation policy for account and role administration."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from obox_builder.contrib.auth.models import Permission, Role, User
from obox_builder.exceptions import PermissionDenied
from obox_builder.security import Actor


def check_permissions(
    actor: Actor, permissions: Sequence[Permission], *, existing: Sequence[Permission] = ()
) -> None:
    if actor.is_active and actor.is_superuser:
        return
    from obox_builder.contrib.auth.permissions import collect_catalog

    declared = collect_catalog().permissions
    retained = {permission.id for permission in existing}
    if any(
        not actor.has_permission(p.name) and not (p.name not in declared and p.id in retained)
        for p in permissions
    ):
        raise PermissionDenied("You cannot administer permissions you do not hold.")


def check_role(actor: Actor, role: Role) -> None:
    check_permissions(actor, role.permissions, existing=role.permissions)


async def selected_roles(db: AsyncSession, actor: Actor, ids: Sequence[int]) -> list[Role]:
    roles = list(
        await db.scalars(
            sa.select(Role)
            .where(Role.id.in_(ids))
            .order_by(Role.id)
            .options(selectinload(Role.permissions))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if {role.id for role in roles} != set(ids):
        raise ValueError("Unknown role.")
    for role in roles:
        check_role(actor, role)
    return roles


async def lock_superusers(db: AsyncSession) -> None:
    # Every demotion takes the same locks in the same order before loading its target.
    await db.execute(
        sa.select(User.id).where(User.is_superuser.is_(True)).order_by(User.id).with_for_update()
    )


async def check_user(
    db: AsyncSession, actor: Actor, target: User | None, *, active: bool, superuser: bool
) -> None:
    if not actor.is_superuser:
        if superuser or (target is not None and target.is_superuser):
            raise PermissionDenied(
                "Only a superuser can change the superuser flag or administer superusers."
            )
        if target is not None:
            for role in target.roles:
                check_role(actor, role)
    if target is not None and target.is_superuser and (not active or not superuser):
        other = await db.scalar(
            sa.select(User.id)
            .where(User.is_superuser.is_(True), User.is_active.is_(True), User.id != target.id)
            .limit(1)
        )
        if other is None:
            raise PermissionDenied("The last active superuser cannot be demoted or deactivated.")
