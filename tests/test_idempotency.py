from __future__ import annotations

import asyncio
from typing import Any

import pytest
from obox.contrib.idempotency import Idempotency
from obox.contrib.idempotency.models import Base
from obox.core.exceptions import BuilderValidationError, NotAuthenticated, ResourceConflict
from obox.core.principal import Principal
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request


def request(key: str | tuple[str, ...] = "receipt-1") -> Request:
    values = (key,) if isinstance(key, str) else key
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/entries",
            "headers": [(b"idempotency-key", value.encode()) for value in values],
        }
    )


def context(
    session: Any, key: str | tuple[str, ...] = "receipt-1", *, authenticated: bool = True
) -> Any:
    return type(
        "Context",
        (),
        {
            "request": request(key),
            "principal": Principal(id=7, is_authenticated=authenticated),
            "session": session,
        },
    )()


def test_idempotency_replays_matching_receipts_and_rejects_key_reuse() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        feature = Idempotency()
        try:
            async with sessions.begin() as session:
                receipt, replay = await feature.begin({"name": "Ada"}, context(session))
                assert receipt is not None
                assert not replay
                receipt.response = {"status": 201, "id": 9}

            async with sessions.begin() as session:
                receipt, replay = await feature.begin({"name": "Ada"}, context(session))
                assert receipt is not None
                assert replay
                assert receipt.response == {"status": 201, "id": 9}

            with pytest.raises(ResourceConflict):
                async with sessions.begin() as session:
                    await feature.begin({"name": "Grace"}, context(session))

            with pytest.raises(NotAuthenticated):
                async with sessions.begin() as session:
                    await feature.begin(
                        {"name": "Ada"}, context(session, "new", authenticated=False)
                    )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_idempotency_requires_a_single_visible_ascii_key() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        sessions = async_sessionmaker(engine)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        try:
            feature = Idempotency()
            with pytest.raises(BuilderValidationError):
                async with sessions.begin() as session:
                    await feature.begin({"name": "Ada"}, context(session, ("first", "second")))
        finally:
            await engine.dispose()

    asyncio.run(scenario())
