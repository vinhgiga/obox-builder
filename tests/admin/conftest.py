from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.conf import SETTINGS_MODULE_ENV
from obox_builder.db.base import ModelBase

# Model classes require a ready app registry at import time (Django-style), and
# test modules import them at module level (see tests/auth/conftest.py).
os.environ.setdefault(SETTINGS_MODULE_ENV, "tests.support.admin_settings")

import obox_builder as _obox_builder  # noqa: E402
from obox_builder.conf import settings as _settings  # noqa: E402
from obox_builder.core.signals import post_migrate as _post_migrate  # noqa: E402

_obox_builder.setup()
_settings.reset()
_obox_builder._reset()
_post_migrate.clear()


def deletion_confirmation(response: httpx.Response) -> str:
    match = re.search(r'name="_delete_confirmation" value="([^"]+)"', response.text)
    assert match is not None
    return match[1]


async def confirmed_delete(
    client: httpx.AsyncClient, url: str, data: dict[str, Any]
) -> httpx.Response:
    preview = await client.post(url, data=data)
    assert preview.status_code == 200, preview.text
    return await client.post(
        url, data={**data, "_delete_confirmation": deletion_confirmation(preview)}
    )


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import obox_builder

    monkeypatch.setenv(SETTINGS_MODULE_ENV, "tests.support.admin_settings")
    obox_builder.setup()


def build_app() -> Any:
    from obox_builder.contrib.admin.site import site
    from obox_builder.core.application import create_app

    application = create_app()
    application.include_router(site.get_router(), prefix="/admin")
    return application


@pytest.fixture
async def client(admin_env: None) -> AsyncIterator[httpx.AsyncClient]:
    from obox_builder.db import dispose, get_engine

    async with get_engine().begin() as connection:
        await connection.run_sync(ModelBase.metadata.drop_all)
        await connection.run_sync(ModelBase.metadata.create_all)
    await ensure_permissions()  # mirrors what post_migrate does in real deployments
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
    await dispose()


@pytest.fixture
async def relation_client(client: httpx.AsyncClient) -> AsyncIterator[httpx.AsyncClient]:
    from obox_builder.contrib.admin import ModelAdmin, TabularInline, site

    from tests.support.apps.shop.models import (
        Asset,
        DetailedMultiTableRecord,
        Invoice,
        Label,
        MultiTableRecord,
        Order,
        OrderDetails,
        OrderLine,
        Shipment,
        Tracking,
    )

    class LineInline(TabularInline):
        model = OrderLine
        fields = ("sku", "quantity", "product_id", "asset_id")
        autocomplete_fields = ("product_id", "asset_id")

    class DetailsInline(TabularInline):
        model = OrderDetails
        fields = ("recipient",)

    class OrderAdmin(ModelAdmin):
        fields = ("reference", "labels")
        autocomplete_fields = ("labels",)
        inlines = (LineInline, DetailsInline)

    class MultiTableRecordAdmin(ModelAdmin):
        fields = ("name",)

    class DetailedMultiTableRecordAdmin(ModelAdmin):
        fields = ("name", "details")

    models = (
        Asset,
        Label,
        DetailedMultiTableRecord,
        MultiTableRecord,
        OrderDetails,
        OrderLine,
        Shipment,
        Tracking,
        Invoice,
        Order,
    )
    previous = {model: site.get_model_admin_for(model) for model in models}
    for model in models:
        search = (
            "title"
            if model is Label
            else "sku"
            if model is OrderLine
            else "recipient"
            if model is OrderDetails
            else "name"
            if model is not Order
            else "reference"
        )
        site.register(
            model,
            OrderAdmin
            if model is Order
            else MultiTableRecordAdmin
            if model is MultiTableRecord
            else DetailedMultiTableRecordAdmin
            if model is DetailedMultiTableRecord
            else type(f"{model.__name__}Admin", (ModelAdmin,), {"search_fields": (search,)}),
        )
    try:
        transport = httpx.ASGITransport(app=build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http
    finally:
        for model, admin in previous.items():
            site.unregister(model)
            if admin is not None:
                site.register(model, type(admin))


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
    is_active: bool = True,
) -> int:
    from obox_builder.contrib.auth import services
    from obox_builder.db.session import session_scope

    async with session_scope() as db:
        user = await services.create_user(db, username, password)
        user.is_superuser = is_superuser
        user.is_active = is_active
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


async def form_login(
    client: httpx.AsyncClient, username: str, password: str, **extra: str
) -> httpx.Response:
    return await client.post(
        "/admin/login",
        data={"username": username, "password": password, **extra},
        follow_redirects=False,
    )


async def csrf_for(username: str, session_token: str | None = None) -> str:
    from obox_builder.contrib.auth.csrf import get_csrf_token
    from obox_builder.contrib.auth.models import Session as AuthSession
    from obox_builder.contrib.auth.sessions import hash_token
    from obox_builder.db.session import session_scope

    async with session_scope() as db:
        if session_token is None:
            user_id = await _user_id(db, username)
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
        assert row is not None  # the cookie set by login_session must still have a row
        return get_csrf_token(row)


async def _user_id(db: Any, username: str) -> int:
    from obox_builder.contrib.auth.models import User

    user_id = await db.scalar(sa.select(User.id).where(User.username == username))
    assert user_id is not None
    return user_id
