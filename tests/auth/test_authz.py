from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
import sqlalchemy as sa
from obox_builder.contrib.auth.authz import (
    Actor,
    AuthorizationCatalog,
    PermissionName,
    authorization_catalog,
    lower_camel,
    model_permission,
)
from obox_builder.contrib.auth.models import Permission, Role, User
from obox_builder.contrib.auth.permissions import get_actor, sync_permissions
from obox_builder.contrib.auth.services import create_user
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.parametrize(
    "name",
    ["auth.users.read", "kpi.productionQuantity.update", "datatable.productionConfirm.bulkUpdate"],
)
def test_permission_name_round_trip(name: str) -> None:
    assert PermissionName.parse(name).name == name


@pytest.mark.parametrize(
    "name",
    [
        "auth.view_user",
        "auth.users",
        "auth.users.read.extra",
        "Auth.users.read",
        "auth.user_roles.read",
        "auth.users.bulk_update",
        "auth.*.read",
        "auth.users.*",
        "auth.users.read\n",
        "auth.users.đọc",
        " auth.users.read",
        "auth.users.",
    ],
)
def test_invalid_permission_names_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="permission|Permissions"):
        PermissionName.parse(name)


def test_model_permissions_group_only_account_writes(auth_env: None) -> None:
    from tests.support.apps.shop.models import Product

    assert {model_permission(User, action) for action in ("add", "change", "delete")} == {
        "auth.users.manage"
    }
    assert model_permission(Role, "read") == "auth.roles.read"
    assert model_permission(Role, "update") == "auth.roles.manage"
    assert model_permission(Product, "create") == "shop.product.create"
    assert model_permission(Product, "change") == "shop.product.update"
    assert model_permission(Product, "publish") == "shop.product.publish"
    assert lower_camel("production_confirm") == "productionConfirm"


def test_actor_is_an_immutable_snapshot() -> None:
    grants = {"auth.users.read"}
    attributes = {"department": "Operations"}
    actor = Actor(id=7, permissions=frozenset(grants), attributes=attributes)
    grants.add("auth.users.manage")
    attributes["department"] = "Other"
    assert actor.has_permission("auth.users.read")
    assert not actor.has_permission("auth.users.manage")
    assert actor.attributes["department"] == "Operations"
    with pytest.raises(FrozenInstanceError):
        actor.is_active = False  # type: ignore[misc]
    with pytest.raises(TypeError):
        actor.attributes["department"] = "Other"  # type: ignore[index]


@pytest.mark.parametrize("is_superuser", [False, True])
def test_inactive_actor_never_has_permissions(is_superuser: bool) -> None:
    actor = Actor(
        permissions=frozenset({"auth.users.manage"}), is_superuser=is_superuser, is_active=False
    )
    assert not actor.has_permission("auth.users.manage")
    assert not actor.has_permission("shop.product.read")


def test_catalog_validates_and_unions_role_declarations() -> None:
    catalog = AuthorizationCatalog()
    read = catalog.register_permission("auth", "users", "read")
    assert catalog.register_permission("auth", "users", "read") == read
    catalog.register_role("Readers", permissions=frozenset({read}))
    catalog.register_role("Readers", permissions=frozenset({"auth.users.manage"}))
    with pytest.raises(ValueError, match="Unknown permissions"):
        catalog.validate()
    catalog.register_permission("auth", "users", "manage")
    catalog.validate()
    assert len(catalog.permissions) == 2
    assert catalog.roles["Readers"] == {read, "auth.users.manage"}
    with pytest.raises(ValueError, match="Role names"):
        catalog.register_role(" ")


async def test_role_templates_preserve_admin_edits(db: AsyncSession) -> None:
    name = authorization_catalog.register_permission("reports", "daily", "read")
    authorization_catalog.register_role("Report readers", permissions=frozenset({name}))
    await sync_permissions(db, dry_run=True)
    assert await db.scalar(sa.select(Role.id).where(Role.name == "Report readers")) is None
    await sync_permissions(db)
    role = await db.scalar(sa.select(Role).where(Role.name == "Report readers"))
    assert role is not None
    await db.refresh(role, ["permissions"])
    assert [permission.name for permission in role.permissions] == [name]
    role.permissions.clear()
    await db.flush()
    await sync_permissions(db)
    await db.refresh(role, ["permissions"])
    assert role.permissions == []


async def test_unknown_role_template_grant_prevents_sync(db: AsyncSession) -> None:
    authorization_catalog.register_role("Invalid", permissions=frozenset({"reports.daily.read"}))
    with pytest.raises(ValueError, match="Unknown permissions"):
        await sync_permissions(db)
    assert (await db.scalars(sa.select(Permission))).all() == []


async def test_actor_resolution_reflects_grant_changes(db: AsyncSession) -> None:
    await sync_permissions(db)
    user = await create_user(db, "reader", "s3cure-password")
    permission = await db.scalar(
        sa.select(Permission).where(
            Permission.service == "shop",
            Permission.resource == "product",
            Permission.action == "read",
        )
    )
    assert permission is not None
    role = Role(name="Readers", permissions=[permission])
    db.add(role)
    await db.refresh(user, ["roles"])
    user.roles.append(role)
    await db.flush()
    before = await get_actor(db, user)
    assert before.roles == {"Readers"}
    assert before.has_permission("shop.product.read")
    role.permissions.clear()
    await db.flush()
    after = await get_actor(db, user)
    assert after.roles == {"Readers"}
    assert after.permissions == frozenset()
    assert before.has_permission("shop.product.read")


async def test_superuser_actor_lists_declared_permissions(db: AsyncSession) -> None:
    user = await create_user(db, "root", "s3cure-password", is_superuser=True)
    actor = await get_actor(db, user)
    assert {"auth.users.read", "auth.users.manage"} <= actor.permissions
    assert actor.has_permission("reports.unregistered.read")
    user.is_active = False
    inactive = await get_actor(db, user)
    assert inactive.permissions == frozenset()
    assert not inactive.has_permission("auth.users.manage")


@pytest.mark.parametrize(
    "component,value",
    [
        ("service", "Auth"),
        ("service", "auth_app"),
        ("resource", "user_roles"),
        ("action", "bulk_update"),
        ("action", "read\n"),
    ],
)
async def test_database_rejects_invalid_permission_components(
    db: AsyncSession, component: str, value: str
) -> None:
    values = {"service": "auth", "resource": "users", "action": "read", component: value}
    with pytest.raises(ValueError, match="Invalid permission"):
        Permission(**values)
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(sa.insert(Permission).values(**values))


async def test_database_rejects_duplicate_permissions(db: AsyncSession) -> None:
    values = {"service": "auth", "resource": "users", "action": "read"}
    db.add(Permission(**values))
    await db.flush()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(sa.insert(Permission).values(**values))
