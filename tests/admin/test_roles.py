from __future__ import annotations

import httpx
import pytest
import sqlalchemy as sa

from tests.admin.conftest import csrf_for, grant_permissions, login_session, make_user

pytestmark = pytest.mark.usefixtures("admin_env")


async def test_list_requires_permission(client: httpx.AsyncClient) -> None:
    await make_user("alice")
    await login_session(client, "alice")
    response = await client.get("/admin/roles", headers={"accept": "text/html"})
    assert response.status_code == 403


async def test_create_and_edit_role_with_permissions(client: httpx.AsyncClient) -> None:
    from obox_builder.contrib.auth.models import Permission, Role
    from obox_builder.db.session import session_scope

    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")

    form = await client.get("/admin/roles/new")
    assert form.status_code == 200
    assert 'name="description"' not in form.text

    created = await client.post(
        "/admin/roles/new",
        data={"csrf_token": token, "name": "Editors"},
        follow_redirects=False,
    )
    assert created.status_code == 303

    async with session_scope() as db:
        role = await db.scalar(sa.select(Role).where(Role.name == "Editors"))
        assert role is not None
        role_id = role.id
        permission = await db.scalar(
            sa.select(Permission).where(
                Permission.app_label == "auth", Permission.codename == "view_user"
            )
        )
        assert permission is not None
        permission_id = permission.id

    updated = await client.post(
        f"/admin/roles/{role_id}",
        data={
            "csrf_token": token,
            "name": "Editors",
            "permission_ids": [str(permission_id)],
        },
        follow_redirects=False,
    )
    assert updated.status_code == 303

    form = await client.get(f"/admin/roles/{role_id}")
    assert form.status_code == 200
    assert 'name="description"' not in form.text
    listing = await client.get("/admin/roles")
    assert listing.status_code == 200
    assert "Editors" in listing.text
    assert "<th>Description</th>" not in listing.text

    async with session_scope() as db:
        role = await db.scalar(sa.select(Role).where(Role.id == role_id).options())
        assert role is not None
        await db.refresh(role, ["permissions"])
        assert [perm.codename for perm in role.permissions] == ["view_user"]


async def test_duplicate_role_name_rejected(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")
    await client.post(
        "/admin/roles/new", data={"csrf_token": token, "name": "Editors"}, follow_redirects=False
    )
    duplicate = await client.post(
        "/admin/roles/new", data={"csrf_token": token, "name": "editors".title()}
    )
    assert duplicate.status_code == 200
    assert "already exists" in duplicate.text


@pytest.mark.parametrize("holds_permission", [False, True])
async def test_delegate_deletes_only_roles_with_held_permissions(
    client: httpx.AsyncClient, holds_permission: bool
) -> None:
    from obox_builder.contrib.auth.models import Permission, Role
    from obox_builder.db.session import session_scope

    await make_user("alice")
    await grant_permissions("alice", "auth.delete_role")
    if holds_permission:
        await grant_permissions("alice", "auth.view_user")
    await login_session(client, "alice")
    token = await csrf_for("alice")
    async with session_scope() as db:
        permission = await db.scalar(
            sa.select(Permission).where(
                Permission.app_label == "auth", Permission.codename == "view_user"
            )
        )
        assert permission is not None
        role = Role(name="Readers", permissions=[permission])
        db.add(role)
        await db.flush()
        role_id = role.id

    response = await client.post(
        f"/admin/roles/{role_id}/delete", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == (303 if holds_permission else 403)
    async with session_scope() as db:
        assert (await db.get(Role, role_id) is None) == holds_permission


async def test_delete_custom_role(client: httpx.AsyncClient) -> None:
    from obox_builder.contrib.auth.models import Role
    from obox_builder.db.session import session_scope

    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")
    async with session_scope() as db:
        role = Role(name="Temporary")
        db.add(role)
        await db.flush()
        role_id = role.id

    response = await client.post(
        f"/admin/roles/{role_id}/delete", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    async with session_scope() as db:
        assert await db.scalar(sa.select(Role).where(Role.id == role_id)) is None


async def test_role_list_requires_add_for_create(client: httpx.AsyncClient) -> None:
    await make_user("alice")
    await grant_permissions("alice", "auth.view_role")
    await login_session(client, "alice")
    listing = await client.get("/admin/roles", headers={"accept": "text/html"})
    assert listing.status_code == 200
    assert "New role" not in listing.text

    create = await client.get("/admin/roles/new", headers={"accept": "text/html"})
    assert create.status_code == 403
