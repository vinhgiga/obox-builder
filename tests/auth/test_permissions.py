from __future__ import annotations

from obox_builder.contrib.auth import permissions as perms
from obox_builder.contrib.auth.models import Permission, Role
from obox_builder.contrib.auth.services import create_user
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def test_collect_permissions_covers_models_and_extras(auth_env: None) -> None:
    names = {entry.full_name for entry in perms.collect_permissions()}
    assert "shop.view_product" in names
    assert "shop.change_category" in names
    assert "shop.publish_product" in names
    assert "auth.change_role" in names
    assert "auth.view_user" in names


def test_collect_permissions_uses_app_labels(auth_env: None) -> None:
    entries = {entry.full_name: entry for entry in perms.collect_permissions()}
    publish = entries["shop.publish_product"]
    assert publish.name == "Can publish product"
    assert publish.model == "product"


async def test_sync_permissions_creates_and_is_idempotent(db: AsyncSession) -> None:
    wanted = {entry.full_name for entry in perms.collect_permissions()}
    result = await perms.sync_permissions(db)
    assert set(result.created) == wanted
    assert result.pruned == []
    rows = (await db.scalars(select(Permission))).all()
    assert {row.full_name for row in rows} == wanted

    again = await perms.sync_permissions(db)
    assert again.created == []
    assert again.unchanged == len(wanted)


async def test_sync_permissions_dry_run_writes_nothing(db: AsyncSession) -> None:
    result = await perms.sync_permissions(db, dry_run=True)
    assert result.created
    assert (await db.scalars(select(Permission))).all() == []


async def test_sync_permissions_prunes_orphans(db: AsyncSession) -> None:
    wanted = {entry.full_name for entry in perms.collect_permissions()}
    await perms.sync_permissions(db)
    orphan = Permission(app_label="legacy", codename="old_thing", name="Old thing")
    db.add(orphan)
    await db.flush()

    result = await perms.sync_permissions(db, prune=True)
    assert "legacy.old_thing" in result.pruned
    remaining = {row.full_name for row in await db.scalars(select(Permission))}
    assert remaining == wanted


async def test_sync_permissions_updates_renamed_permissions(db: AsyncSession) -> None:
    await perms.sync_permissions(db)
    row = await db.scalar(
        select(Permission).where(
            Permission.app_label == "shop", Permission.codename == "view_product"
        )
    )
    assert row is not None
    row.name = "Old name"
    await db.flush()
    await perms.sync_permissions(db)
    refreshed = await db.scalar(
        select(Permission).where(
            Permission.app_label == "shop", Permission.codename == "view_product"
        )
    )
    assert refreshed is not None
    assert refreshed.name == "Can view product"


async def test_get_user_permissions_unions_roles(db: AsyncSession) -> None:
    await perms.sync_permissions(db)
    user = await create_user(db, "carol", "s3cure-password")
    editor = Role(name="Editor")
    viewer = Role(name="Viewer")
    perms_by_name = {row.full_name: row for row in await db.scalars(select(Permission))}
    editor.permissions.append(perms_by_name["shop.view_product"])
    viewer.permissions.append(perms_by_name["shop.view_category"])
    db.add_all([editor, viewer])
    await db.flush()
    await db.refresh(user, ["roles"])
    user.roles.extend([editor, viewer])
    await db.flush()

    effective = await perms.get_user_permissions(db, user.id)
    assert effective == {"shop.view_product", "shop.view_category"}


async def test_superuser_bypasses_permission_checks(db: AsyncSession) -> None:
    user = await create_user(db, "root", "s3cure-password", is_superuser=True)
    user.permissions = await perms.get_user_permissions(db, user.id)
    assert user.permissions == frozenset()
    assert user.has_perm("anything.at_all")


async def test_regular_user_defaults_to_deny(db: AsyncSession) -> None:
    user = await create_user(db, "dave", "s3cure-password")
    user.permissions = await perms.get_user_permissions(db, user.id)
    assert not user.has_perm("shop.view_product")
