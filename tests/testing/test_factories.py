from __future__ import annotations

import importlib
import importlib.metadata
import sys

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.contrib.auth import services
from obox_builder.contrib.auth.models import Role, User
from obox_builder.db.session import session_scope
from obox_builder.testing import (
    DEFAULT_PASSWORD,
    build_client,
    grant_permissions,
    make_role,
    make_user,
)
from sqlalchemy.orm import selectinload

nova = "nova"
alice = "alice"


async def test_make_user_creates_user(db_ready: None) -> None:
    user = await make_user(alice, email="Alice@Example.ORG")
    assert user.id is not None
    assert user.username == alice
    assert user.email == "alice@example.org"
    async with session_scope() as db:
        assert await services.authenticate(db, alice, DEFAULT_PASSWORD) is not None


async def test_make_user_is_idempotent_and_updates_flags(db_ready: None) -> None:
    first = await make_user(alice)
    second = await make_user(alice, password="another-pass-123", is_superuser=True)
    assert first.id == second.id
    async with session_scope() as db:
        assert await services.authenticate(db, alice, "another-pass-123") is not None
        row = await db.get(User, first.id)
        assert row is not None
        assert row.is_superuser


async def test_make_role_resolves_permissions(db_ready: None) -> None:
    await make_role("Editor", "auth.view_user", "auth.change_user", is_system=False)
    async with session_scope() as db:
        row = await db.scalar(
            sa.select(Role).where(Role.name == "Editor").options(selectinload(Role.permissions))
        )
        assert row is not None
        names = sorted(f"{p.app_label}.{p.codename}" for p in row.permissions)
        assert names == ["auth.change_user", "auth.view_user"]


async def test_make_role_rejects_unknown_permission(db_ready: None) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        await make_role("Broken", "nope.not_a_perm")


async def test_grant_permissions_attaches_personal_role(db_ready: None) -> None:
    user = await make_user(nova)
    await grant_permissions(user, "auth.view_user")
    async with session_scope() as db:
        row = await db.get(User, user.id)
        assert row is not None
        await db.refresh(row, ["roles"])
        assert [role.name for role in row.roles] == [f"{nova}-role"]
    assert await user_has_perm_permission(user.id, "auth", "view_user")


async def user_has_perm_permission(user_id: int, app_label: str, codename: str) -> bool:
    async with session_scope() as db:
        row = await db.get(User, user_id)
        assert row is not None
        await db.refresh(row, ["roles"])
        for role in row.roles:
            await db.refresh(role, ["permissions"])
        return any(
            permission.app_label == app_label and permission.codename == codename
            for role in row.roles
            for permission in role.permissions
        )


async def test_duplicate_grant_permissions_does_not_duplicate(db_ready: None) -> None:
    user = await make_user(alice)
    await grant_permissions(user, "auth.view_user")
    await grant_permissions(user, "auth.view_user", "auth.change_user")

    async def count_roles() -> int:
        from obox_builder.contrib.auth.models import Role

        async with session_scope() as db:
            return len(
                (await db.scalars(sa.select(Role).where(Role.name == f"{alice}-role"))).all()
            )

    assert await count_roles() == 1
    assert await user_has_perm_permission(user.id, "auth", "change_user")


async def test_build_client_uses_asgi_transport(db_ready: None) -> None:
    async with build_client() as http:
        assert isinstance(http._transport, httpx.ASGITransport)  # noqa: SLF001


def test_plugin_registered_as_pytest11_entry_point() -> None:
    entries = importlib.metadata.entry_points(group="pytest11")
    assert any(entry.value == "obox_builder.testing.pytest_plugin" for entry in entries)


def test_plugin_imports_without_settings_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OBOX_SETTINGS_MODULE", raising=False)
    monkeypatch.delitem(sys.modules, "obox_builder.testing.pytest_plugin", raising=False)
    module = importlib.import_module("obox_builder.testing.pytest_plugin")
    assert hasattr(module, "obox_env")
