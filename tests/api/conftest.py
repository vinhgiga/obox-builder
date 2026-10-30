from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import obox_builder
import pytest
from fastapi import FastAPI
from obox_builder.conf import settings
from obox_builder.contrib.api import Api
from obox_builder.db import dispose, get_engine, get_sessionmaker
from obox_builder.db.base import ModelBase
from obox_builder.resources import get_registry
from tests.support.auth_settings import Settings as AuthSettings


class FeatureSettings(AuthSettings):
    INSTALLED_APPS: list[str] = [
        "obox_builder.contrib.auth",
        "obox_builder.contrib.api",
        "obox_builder.contrib.history",
        "obox_builder.contrib.comments",
        "obox_builder.contrib.savedviews",
        "tests.support.apps.shop",
        "tests.support.apps.warehouse",
        "tests.support.apps.commentsdemo",
    ]


@pytest.fixture
async def feature_api() -> AsyncIterator[httpx.AsyncClient]:
    settings.configure(FeatureSettings)
    obox_builder.setup()
    assert not obox_builder.apps.is_installed("obox_builder.contrib.admin")
    async with get_engine().begin() as conn:
        await conn.run_sync(ModelBase.metadata.drop_all)
        await conn.run_sync(ModelBase.metadata.create_all)
    from obox_builder.contrib.auth.permissions import sync_permissions

    async with get_sessionmaker()() as db:
        await sync_permissions(db)
        await db.commit()
    api = Api()
    for resource in get_registry().resources.values():
        api.register(type(resource))
    app = FastAPI()
    app.include_router(api.get_router(), prefix="/api")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as http:
        yield http
    await dispose()


async def sign_in(
    api: httpx.AsyncClient, *, username: str = "root", superuser: bool = True
) -> dict[str, str]:
    from obox_builder.contrib.auth.services import create_user

    async with get_sessionmaker()() as db:
        user = await create_user(db, username, "s3cure-password", is_superuser=superuser)
        await db.commit()
        assert user.id
    response = await api.post(
        "/api/auth/login", json={"username": username, "password": "s3cure-password"}
    )
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrf_token"]}


async def grant(username: str, *names: str) -> None:
    import sqlalchemy as sa
    from obox_builder.contrib.auth.models import Permission, Role, User
    from sqlalchemy.orm import selectinload

    async with get_sessionmaker()() as db:
        user = await db.scalar(
            sa.select(User).where(User.username == username).options(selectinload(User.roles))
        )
        assert user is not None
        permissions = list(
            await db.scalars(
                sa.select(Permission).where(
                    (Permission.service + "." + Permission.resource + "." + Permission.action).in_(
                        names
                    )
                )
            )
        )
        assert len(permissions) == len(names)
        user.roles.append(Role(name=username + " grants", permissions=permissions))
        await db.commit()


async def create_item(
    api: httpx.AsyncClient, headers: dict[str, str], **values: Any
) -> httpx.Response:
    response = await api.post(
        "/api/warehouse/item",
        headers=headers,
        json={"name": "Original", "sku": "sku-1", **values},
    )
    assert response.status_code == 201, response.text
    return response
