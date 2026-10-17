from __future__ import annotations

import httpx
import pytest
import sqlalchemy as sa

from tests.admin.conftest import csrf_for, grant_permissions, login_session, make_user

pytestmark = pytest.mark.usefixtures("admin_env")


async def test_list_requires_permission(client: httpx.AsyncClient) -> None:
    await make_user("alice")
    await login_session(client, "alice")
    response = await client.get("/admin/users", headers={"accept": "text/html"})
    assert response.status_code == 403
    assert "Permission denied" in response.text


async def test_list_shows_users_and_search(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await make_user("bob")
    await login_session(client, "alice")
    response = await client.get("/admin/users", headers={"accept": "text/html"})
    assert response.status_code == 200
    assert "alice" in response.text
    assert "bob" in response.text

    filtered = await client.get("/admin/users?q=bob", headers={"accept": "text/html"})
    assert 'href="/admin/users/2"' in filtered.text
    assert 'href="/admin/users/1"' not in filtered.text


async def test_create_user_via_form(client: httpx.AsyncClient) -> None:
    from obox_builder.contrib.auth.models import Role, User
    from obox_builder.db.session import session_scope

    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")
    async with session_scope() as db:
        db.add(Role(name="Editors"))
    response = await client.post(
        "/admin/users/new",
        data={
            "csrf_token": token,
            "username": "carol",
            "email": "carol@example.com",
            "full_name": "Carol Doe",
            "password": "s3cure-password",
            "confirm_password": "s3cure-password",
            "is_active": "true",
            "role_ids": ["1"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/users"
    async with session_scope() as db:
        carol = await db.scalar(sa.select(User).where(User.username == "carol"))
        assert carol is not None
        assert carol.email == "carol@example.com"
        assert carol.is_active
        await db.refresh(carol, ["roles"])
        assert [role.name for role in carol.roles] == ["Editors"]


async def test_create_rejects_password_mismatch(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")
    response = await client.post(
        "/admin/users/new",
        data={
            "csrf_token": token,
            "username": "carol",
            "password": "s3cure-password",
            "confirm_password": "different-password",
            "is_active": "true",
        },
    )
    assert response.status_code == 200
    assert "do not match" in response.text


async def test_create_requires_csrf(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    response = await client.post(
        "/admin/users/new",
        data={"username": "carol", "password": "s3cure-password"},
    )
    assert response.status_code == 403


async def test_edit_assigns_roles_and_deactivation_revokes_sessions(
    client: httpx.AsyncClient,
) -> None:
    from obox_builder.contrib.auth.models import Role
    from obox_builder.contrib.auth.models import Session as AuthSession
    from obox_builder.db.session import session_scope

    await make_user("alice", is_superuser=True)
    await grant_permissions("alice", "auth.view_user", "auth.change_user")
    await make_user("bob")
    async with session_scope() as db:
        db.add(Role(name="Editors"))
    bob_token = await login_session(client, "bob")
    await login_session(client, "alice")
    token = await csrf_for("alice")

    from obox_builder.contrib.auth.models import User

    async with session_scope() as db:
        bogus = await db.scalar(sa.select(User).where(User.username == "bob"))
        assert bogus is not None
        role = await db.scalar(sa.select(Role).where(Role.name == "Editors"))
        assert role is not None
        role_id = role.id
        bob_id = bogus.id

    response = await client.post(
        f"/admin/users/{bob_id}",
        data={
            "csrf_token": token,
            "email": "bob@example.com",
            "full_name": "Bob",
            "role_ids": [str(role_id)],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    async with session_scope() as db:
        bob = await db.scalar(sa.select(User).where(User.id == bob_id).options())
        assert bob is not None
        await db.refresh(bob, ["roles"])
        assert [role.name for role in bob.roles] == ["Editors"]
        assert bob.is_active is False  # unchecked checkbox deactivates
        remaining = list(await db.scalars(sa.select(AuthSession)))
        assert all(row.token_hash not in {bob_token} for row in remaining)


async def test_last_superuser_cannot_be_demoted(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")

    from obox_builder.contrib.auth.models import User
    from obox_builder.db.session import session_scope

    async with session_scope() as db:
        alice = await db.scalar(sa.select(User).where(User.username == "alice"))
        assert alice is not None
        alice_id = alice.id

    response = await client.post(
        f"/admin/users/{alice_id}",
        data={"csrf_token": token, "email": "", "is_active": "true"},
    )
    assert response.status_code == 200
    assert "last active superuser" in response.text


async def test_non_superuser_cannot_toggle_superuser_flag(client: httpx.AsyncClient) -> None:
    await make_user("alice")
    await grant_permissions("alice", "auth.view_user", "auth.change_user")
    await make_user("bob")
    await login_session(client, "alice")
    token = await csrf_for("alice")

    from obox_builder.contrib.auth.models import User
    from obox_builder.db.session import session_scope

    async with session_scope() as db:
        bob = await db.scalar(sa.select(User).where(User.username == "bob"))
        assert bob is not None
        bob_id = bob.id

    response = await client.post(
        f"/admin/users/{bob_id}",
        data={"csrf_token": token, "email": "", "is_active": "true", "is_superuser": "true"},
    )
    assert response.status_code == 200
    assert "superuser flag" in response.text
