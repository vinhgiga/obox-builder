"""Pytest fixtures for obox-builder projects.

Registered automatically through the ``pytest11`` entry point. Requires
``pytest-asyncio`` with ``asyncio_mode = "auto"`` (async fixtures/tests) and an
``OBOX_SETTINGS_MODULE`` environment variable (or ``OBOX_SETTINGS_MODULE`` set
by the project's own ``conftest.py`` before fixtures run).

Fixtures:

- ``obox_env``: loads the settings module and calls ``obox_builder.setup()``.
- ``db``: drops and recreates every registered table, syncs permissions, yields
  an ``AsyncSession``.
- ``client``: an ``httpx.AsyncClient`` against the project's ASGI app.
- ``login``: async helper that creates a user, starts a session and sets the
  session cookie on ``client``.

The module imports only stdlib/pytest/httpx at import time so that pytest can
load the plugin before any settings module is configured.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.testing.client import build_client, session_cookie


def _settings_module() -> str:
    module = os.environ.get("OBOX_SETTINGS_MODULE", "")
    if not module:
        raise pytest.UsageError(
            "obox-builder pytest plugin needs OBOX_SETTINGS_MODULE (export it or set it "
            "in the project's conftest.py before the fixtures run)."
        )
    return module


@pytest.fixture
def obox_env() -> None:
    import obox_builder

    _settings_module()
    obox_builder.setup()


@pytest.fixture
async def db(obox_env: None) -> AsyncIterator[AsyncSession]:
    from obox_builder.contrib.auth.permissions import sync_permissions
    from obox_builder.db import get_engine, get_sessionmaker
    from obox_builder.db.base import ModelBase

    async with get_engine().begin() as connection:
        await connection.run_sync(ModelBase.metadata.drop_all)
        await connection.run_sync(ModelBase.metadata.create_all)
    async with get_sessionmaker()() as session:
        await sync_permissions(session)  # mirrors what post_migrate does in production
        await session.commit()
    async with get_sessionmaker()() as session:
        yield session
        await session.rollback()


@pytest.fixture
async def client(db: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    async with build_client() as http:
        yield http


@pytest.fixture
def login(client: httpx.AsyncClient) -> Callable[..., Awaitable[str]]:
    from obox_builder.contrib.auth import sessions
    from obox_builder.db.session import session_scope
    from obox_builder.testing.factories import DEFAULT_PASSWORD, make_user

    async def _login(
        username: str,
        password: str = DEFAULT_PASSWORD,
        **user_kwargs: Any,
    ) -> str:
        user = await make_user(username, password=password, **user_kwargs)
        async with session_scope() as session:
            token, _row = await sessions.create_session(session, user.id)
        session_cookie(client, token)
        return token

    return _login
