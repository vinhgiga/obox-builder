from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import obox_builder
import pytest
from obox_builder.conf import SETTINGS_MODULE_ENV, settings
from obox_builder.contrib.admin.site import site
from obox_builder.db import dispose, get_engine, get_sessionmaker
from obox_builder.db.base import ModelBase
from sqlalchemy.ext.asyncio import AsyncSession

from tests.admin.conftest import build_app, ensure_permissions


@pytest.fixture
async def imports_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[None]:
    monkeypatch.setenv(SETTINGS_MODULE_ENV, "tests.support.imports_settings")
    obox_builder.setup()
    monkeypatch.setattr(
        settings.load(),
        "DATABASE_URL",
        os.environ.get(
            "IMPORT_TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'imports.db'}"
        ),
    )
    from tests.support.apps.shop.models import Order, Product

    for model, keys in ((Order, ("reference",)), (Product, ("id",))):
        admin = site.get_model_admin_for(model)
        assert admin is not None
        monkeypatch.setattr(admin, "import_enabled", True)
        monkeypatch.setattr(admin, "import_keys", keys)
    async with get_engine().begin() as connection:
        await connection.run_sync(ModelBase.metadata.drop_all)
        await connection.run_sync(ModelBase.metadata.create_all)
    await ensure_permissions()
    try:
        yield
    finally:
        await dispose()


@pytest.fixture
async def db(imports_env: None) -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session
        await session.rollback()


@pytest.fixture
async def client(imports_env: None) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app()), base_url="http://testserver"
    ) as client:
        yield client
