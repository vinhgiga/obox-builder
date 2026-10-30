from __future__ import annotations

from obox_builder.contrib.auth import permissions as perms
from obox_builder.contrib.auth.models import Permission, Role
from obox_builder.contrib.auth.services import create_user
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def test_collect_permissions_covers_models_and_extras(auth_env: None) -> None:
    names = {entry.name for entry in perms.collect_permissions()}
    assert "shop.product.read" in names
    assert "shop.category.update" in names
    assert "shop.product.publish" in names
    assert "auth.roles.manage" in names
    assert "auth.users.read" in names


def test_collect_permissions_uses_app_labels(auth_env: None) -> None:
    entries = {entry.name: entry for entry in perms.collect_permissions()}
    publish = entries["shop.product.publish"]
    assert publish.service == "shop"
    assert publish.resource == "product"
    assert publish.action == "publish"


async def test_sync_permissions_creates_and_is_idempotent(db: AsyncSession) -> None:
    wanted = {entry.name for entry in perms.collect_permissions()}
    result = await perms.sync_permissions(db)
    assert set(result.created) == wanted
    assert result.pruned == []
    rows = (await db.scalars(select(Permission))).all()
    assert {row.name for row in rows} == wanted

    again = await perms.sync_permissions(db)
    assert again.created == []
    assert again.unchanged == len(wanted)


async def test_sync_permissions_dry_run_writes_nothing(db: AsyncSession) -> None:
    result = await perms.sync_permissions(db, dry_run=True)
    assert result.created
    assert (await db.scalars(select(Permission))).all() == []


async def test_sync_permissions_prunes_orphans(db: AsyncSession) -> None:
    wanted = {entry.name for entry in perms.collect_permissions()}
    await perms.sync_permissions(db)
    orphan = Permission(service="legacy", resource="thing", action="old")
    db.add(orphan)
    await db.flush()

    result = await perms.sync_permissions(db, prune=True)
    assert "legacy.thing.old" in result.pruned
    remaining = {row.name for row in await db.scalars(select(Permission))}
    assert remaining == wanted


async def test_sync_permissions_preserves_existing_grants(db: AsyncSession) -> None:
    await perms.sync_permissions(db)
    row = await db.scalar(
        select(Permission).where(
            Permission.service == "shop",
            Permission.resource == "product",
            Permission.action == "read",
        )
    )
    assert row is not None
    role = Role(name="Readers", permissions=[row])
    db.add(role)
    await db.flush()
    await perms.sync_permissions(db)
    refreshed = await db.scalar(
        select(Permission).where(
            Permission.service == "shop",
            Permission.resource == "product",
            Permission.action == "read",
        )
    )
    assert refreshed is not None
    assert refreshed.id == row.id
    await db.refresh(role, ["permissions"])
    assert [permission.id for permission in role.permissions] == [row.id]


async def test_get_user_permissions_unions_roles(db: AsyncSession) -> None:
    await perms.sync_permissions(db)
    user = await create_user(db, "carol", "s3cure-password")
    editor = Role(name="Editor")
    viewer = Role(name="Viewer")
    perms_by_name = {row.name: row for row in await db.scalars(select(Permission))}
    editor.permissions.append(perms_by_name["shop.product.read"])
    viewer.permissions.append(perms_by_name["shop.category.read"])
    db.add_all([editor, viewer])
    await db.flush()
    await db.refresh(user, ["roles"])
    user.roles.extend([editor, viewer])
    await db.flush()

    effective = await perms.get_user_permissions(db, user.id)
    assert effective == {"shop.product.read", "shop.category.read"}


async def test_superuser_has_only_declared_permissions(db: AsyncSession) -> None:
    user = await create_user(db, "root", "s3cure-password", is_superuser=True)
    user.permissions = await perms.get_user_permissions(db, user.id)
    assert user.permissions == frozenset()
    assert not user.has_perm("anything.at.all")
    assert user.has_perm("auth.users.manage")


async def test_regular_user_defaults_to_deny(db: AsyncSession) -> None:
    user = await create_user(db, "dave", "s3cure-password")
    user.permissions = await perms.get_user_permissions(db, user.id)
    assert not user.has_perm("shop.product.read")
