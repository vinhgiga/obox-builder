from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import Depends, FastAPI, Request
from obox_builder.conf import settings
from obox_builder.contrib.auth import sessions
from obox_builder.contrib.auth.dependencies import (
    CurrentActor,
    CurrentUser,
    any_permission_required,
    get_current_user,
    login_required,
    permission_required,
    require_permission,
)
from obox_builder.contrib.auth.models import Session as AuthSession
from obox_builder.contrib.auth.models import User
from obox_builder.contrib.auth.services import create_user
from obox_builder.core.application import create_app
from obox_builder.db.base import ModelBase
from obox_builder.db.session import session_scope


def build_app() -> FastAPI:
    application = create_app()

    @application.get("/api/actor", dependencies=[require_permission("shop.product.read")])
    async def actor_info(actor: CurrentActor, user: CurrentUser) -> dict[str, Any]:
        return {
            "id": actor.id,
            "username": user.username,
            "roles": sorted(actor.roles),
            "permissions": sorted(actor.permissions),
        }

    @application.get("/api/me")
    async def me(user: User = Depends(login_required)) -> dict[str, str]:
        return {"username": user.username}

    @application.get(
        "/api/products",
        dependencies=[Depends(permission_required("shop.product.read"))],
    )
    async def products() -> dict[str, bool]:
        return {"ok": True}

    @application.get(
        "/api/all",
        dependencies=[Depends(permission_required("shop.product.read", "shop.product.delete"))],
    )
    async def all_permissions() -> dict[str, bool]:
        return {"ok": True}

    @application.get(
        "/api/any",
        dependencies=[Depends(any_permission_required("shop.product.read", "shop.product.delete"))],
    )
    async def any_permissions() -> dict[str, bool]:
        return {"ok": True}

    @application.get("/api/state")
    async def state(request: Request, user: User = Depends(get_current_user)) -> dict[str, Any]:
        from obox_builder.contrib.auth.context_processors import auth

        context = auth(request)
        return {
            "username": context["user"].username,
            "can_view": bool(context["perms"].shop.product.read),
            "can_delete": bool(context["perms"].shop.product.delete),
        }

    return application


@pytest.fixture
async def client(auth_env: None) -> AsyncIterator[httpx.AsyncClient]:
    from obox_builder.db import get_engine

    async with get_engine().begin() as connection:
        await connection.run_sync(ModelBase.metadata.drop_all)
        await connection.run_sync(ModelBase.metadata.create_all)
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


async def login(client: httpx.AsyncClient, username: str) -> str:
    async with session_scope() as db:
        user = await create_user(db, username, "s3cure-password")
        token, _row = await sessions.create_session(db, user.id)
    client.cookies.set(settings.SESSION_COOKIE_NAME, token)
    return token


async def grant(client: httpx.AsyncClient, username: str, *permissions: str) -> None:
    from obox_builder.contrib.auth.models import Role

    async with session_scope() as db:
        role = Role(name=f"{username}-role")
        for name in permissions:
            service, resource, action = name.split(".")
            perm = await db.scalar(select_permission(service, resource, action))
            assert perm is not None
            role.permissions.append(perm)
        db.add(role)
        await db.flush()
        user = await db.scalar(sa.select(User).where(User.username == username))
        assert user is not None
        await db.refresh(user, ["roles"])
        user.roles.append(role)


def select_permission(service: str, resource: str, action: str) -> Any:
    from obox_builder.contrib.auth.models import Permission

    return sa.select(Permission).where(
        Permission.service == service, Permission.resource == resource, Permission.action == action
    )


async def ensure_permissions() -> None:
    from obox_builder.contrib.auth.permissions import sync_permissions

    async with session_scope() as db:
        await sync_permissions(db)


async def test_current_actor_is_cached_per_request_and_grants_refresh(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obox_builder.contrib.auth import dependencies
    from obox_builder.contrib.auth.models import Role
    from obox_builder.contrib.auth.permissions import get_actor
    from obox_builder.security import Actor

    calls = 0

    async def resolve_actor(db: Any, user: Any) -> Actor:
        nonlocal calls
        calls += 1
        return await get_actor(db, user)

    monkeypatch.setattr(dependencies, "get_actor", resolve_actor)
    await ensure_permissions()
    await login(client, "alice")
    assert (await client.get("/api/actor")).status_code == 403
    await grant(client, "alice", "shop.product.read")
    before = calls
    response = await client.get("/api/actor")
    assert response.status_code == 200
    assert calls == before + 1
    assert response.json()["roles"] == ["alice-role"]
    assert response.json()["permissions"] == ["shop.product.read"]
    assert response.json()["username"] == "alice"
    async with session_scope() as db:
        role = await db.scalar(sa.select(Role).where(Role.name == "alice-role"))
        assert role is not None
        await db.refresh(role, ["permissions"])
        role.permissions.clear()
    assert (await client.get("/api/actor")).status_code == 403


async def test_json_clients_get_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/me", headers={"accept": "application/json"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication required."


async def test_html_clients_get_redirect_to_login(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/me", headers={"accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith(settings.LOGIN_URL)
    assert "next=/api/me" in response.headers["location"]


async def test_valid_session_authenticates(client: httpx.AsyncClient) -> None:
    await login(client, "alice")
    response = await client.get("/api/me")
    assert response.status_code == 200
    assert response.json() == {"username": "alice"}


async def test_unknown_cookie_is_rejected_and_cleared(
    client: httpx.AsyncClient, auth_env: None
) -> None:
    client.cookies.set(settings.SESSION_COOKIE_NAME, "bogus")
    response = await client.get("/api/me")
    assert response.status_code == 401
    assert settings.SESSION_COOKIE_NAME in response.headers.get("set-cookie", "")


async def test_inactive_user_is_rejected_and_sessions_deleted(
    client: httpx.AsyncClient, auth_env: None
) -> None:
    token = await login(client, "bob")
    async with session_scope() as db:
        user = await db.scalar(sa.select(User).where(User.username == "bob"))
        assert user is not None
        user.is_active = False
    response = await client.get("/api/me")
    assert response.status_code == 401
    async with session_scope() as db:
        row = await db.scalar(
            sa.select(AuthSession).where(AuthSession.token_hash == sessions.hash_token(token))
        )
        assert row is None


async def test_permission_required_denies_without_role(client: httpx.AsyncClient) -> None:
    await login(client, "carol")
    response = await client.get("/api/products")
    assert response.status_code == 403
    assert "permission" in response.json()["detail"]


async def test_permission_required_allows_with_role(
    client: httpx.AsyncClient, auth_env: None
) -> None:
    await ensure_permissions()
    await login(client, "dave")
    await grant(client, "dave", "shop.product.read")
    response = await client.get("/api/products")
    assert response.status_code == 200


async def test_permission_required_allows_superusers(
    client: httpx.AsyncClient, auth_env: None
) -> None:
    async with session_scope() as db:
        user = await create_user(db, "root", "s3cure-password", is_superuser=True)
        token, _row = await sessions.create_session(db, user.id)
    client.cookies.set(settings.SESSION_COOKIE_NAME, token)
    response = await client.get("/api/products")
    assert response.status_code == 200


async def test_context_processor_exposes_perms(client: httpx.AsyncClient, auth_env: None) -> None:
    await ensure_permissions()
    await login(client, "erin")
    await grant(client, "erin", "shop.product.read")
    response = await client.get("/api/state")
    assert response.status_code == 200
    assert response.json() == {"username": "erin", "can_view": True, "can_delete": False}


async def test_all_and_any_permissions_are_distinct(client: httpx.AsyncClient) -> None:
    await ensure_permissions()
    await login(client, "alice")
    await grant(client, "alice", "shop.product.read")
    assert (await client.get("/api/any")).status_code == 200
    assert (await client.get("/api/all")).status_code == 403


def test_inactive_user_has_no_permissions() -> None:
    user = User(username="alice", is_active=False, is_superuser=True)
    user.permissions = frozenset({"shop.product.read"})
    assert not user.has_perm("shop.product.read")
