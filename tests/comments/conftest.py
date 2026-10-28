from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.conf import SETTINGS_MODULE_ENV
from obox_builder.db.base import ModelBase

_previous_settings_module = os.environ.get(SETTINGS_MODULE_ENV)
os.environ[SETTINGS_MODULE_ENV] = "tests.support.comments_settings"

import obox_builder as _obox_builder  # noqa: E402
from obox_builder.conf import settings as _settings  # noqa: E402
from obox_builder.core.signals import post_migrate as _post_migrate  # noqa: E402

_obox_builder.setup()
import tests.support.apps.commentsdemo.admin  # noqa: E402,F401  (register admins)

_settings.reset()
_obox_builder._reset()
_post_migrate.clear()

if _previous_settings_module is None:
    os.environ.pop(SETTINGS_MODULE_ENV, None)
else:
    os.environ[SETTINGS_MODULE_ENV] = _previous_settings_module


@pytest.fixture
def comments_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import obox_builder

    monkeypatch.setenv(SETTINGS_MODULE_ENV, "tests.support.comments_settings")
    obox_builder.setup()


@pytest.fixture
async def db(comments_env: None) -> AsyncIterator[Any]:
    from obox_builder.db import dispose, get_engine, get_sessionmaker

    async with get_engine().begin() as connection:
        await connection.run_sync(ModelBase.metadata.drop_all)
        await connection.run_sync(ModelBase.metadata.create_all)
    async with get_sessionmaker()() as session:
        yield session
        await session.rollback()
    await dispose()


def build_app() -> Any:
    from obox_builder.contrib.admin.site import site
    from obox_builder.core.application import create_app

    application = create_app()
    application.include_router(site.get_router(), prefix="/admin")
    return application


@pytest.fixture
async def client(comments_env: None) -> AsyncIterator[httpx.AsyncClient]:
    from obox_builder.db import get_engine

    async with get_engine().begin() as connection:
        await connection.run_sync(ModelBase.metadata.drop_all)
        await connection.run_sync(ModelBase.metadata.create_all)
    await ensure_permissions()
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


async def ensure_permissions() -> None:
    from obox_builder.contrib.auth.permissions import sync_permissions
    from obox_builder.db.session import session_scope

    async with session_scope() as db:
        await sync_permissions(db)


async def make_user(
    username: str,
    *,
    password: str = "s3cure-password",  # noqa: S107
    is_superuser: bool = False,
) -> int:
    from obox_builder.contrib.auth import services
    from obox_builder.db.session import session_scope

    async with session_scope() as db:
        user = await services.create_user(db, username, password)
        user.is_superuser = is_superuser
        await db.flush()
        return user.id


async def grant_permissions(username: str, *permissions: str) -> None:
    await ensure_permissions()
    from obox_builder.contrib.auth.models import Permission, Role, User
    from obox_builder.db.session import session_scope

    async with session_scope() as db:
        role = await db.scalar(sa.select(Role).where(Role.name == f"{username}-role"))
        if role is None:
            role = Role(name=f"{username}-role")
            db.add(role)
            await db.flush()
        await db.refresh(role, ["permissions"])
        for name in permissions:
            service, resource, action = name.split(".")
            perm = await db.scalar(
                sa.select(Permission).where(
                    Permission.service == service,
                    Permission.resource == resource,
                    Permission.action == action,
                )
            )
            assert perm is not None
            if perm not in role.permissions:
                role.permissions.append(perm)
        await db.flush()
        user = await db.scalar(sa.select(User).where(User.username == username))
        assert user is not None
        await db.refresh(user, ["roles"])
        if role not in user.roles:
            user.roles.append(role)


async def login_session(client: httpx.AsyncClient, username: str) -> str:
    from obox_builder.contrib.auth import sessions
    from obox_builder.contrib.auth.models import User
    from obox_builder.db.session import session_scope

    async with session_scope() as db:
        user = await db.scalar(sa.select(User).where(User.username == username))
        assert user is not None
        token, _row = await sessions.create_session(db, user.id)
    client.cookies.set(_settings.SESSION_COOKIE_NAME, token)
    return token


async def csrf_for(username: str, session_token: str | None = None) -> str:
    from obox_builder.contrib.auth.csrf import get_csrf_token
    from obox_builder.contrib.auth.models import Session as AuthSession
    from obox_builder.contrib.auth.models import User
    from obox_builder.contrib.auth.sessions import hash_token
    from obox_builder.db.session import session_scope

    async with session_scope() as db:
        if session_token is None:
            user_id = await db.scalar(sa.select(User.id).where(User.username == username))
            row = await db.scalar(
                sa.select(AuthSession)
                .where(AuthSession.user_id == user_id)
                .order_by(AuthSession.id.desc())
                .limit(1)
            )
        else:
            row = await db.scalar(
                sa.select(AuthSession).where(AuthSession.token_hash == hash_token(session_token))
            )
        assert row is not None
        return get_csrf_token(row)
