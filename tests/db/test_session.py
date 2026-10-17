from __future__ import annotations

import contextlib

import pytest
import sqlalchemy as sa
from obox_builder.conf import SETTINGS_MODULE_ENV
from obox_builder.db import get_db, session_scope
from obox_builder.db.base import ModelBase


@pytest.fixture
def populated(monkeypatch: pytest.MonkeyPatch) -> None:
    import obox_builder

    monkeypatch.setenv(SETTINGS_MODULE_ENV, "tests.support.db_settings")
    obox_builder.setup()


async def _create_tables() -> None:
    from obox_builder.db import get_engine

    async with get_engine().begin() as connection:
        await connection.run_sync(ModelBase.metadata.drop_all)
        await connection.run_sync(ModelBase.metadata.create_all)


async def _product_names() -> list[str]:
    from tests.support.apps.shop.models import Product

    async with session_scope() as session:
        result = await session.execute(sa.select(Product.name))
        return list(result.scalars())


async def test_session_scope_commits(populated: None) -> None:
    from tests.support.apps.shop.models import Product

    await _create_tables()
    async with session_scope() as session:
        session.add(Product(name="Widget", price_cents=100))
    assert await _product_names() == ["Widget"]


async def test_session_scope_rolls_back_on_error(populated: None) -> None:
    from tests.support.apps.shop.models import Product

    await _create_tables()
    with pytest.raises(RuntimeError, match="boom"):
        async with session_scope() as session:
            session.add(Product(name="Doomed"))
            raise RuntimeError("boom")
    assert await _product_names() == []


async def test_get_db_commits_on_clean_exit(populated: None) -> None:
    from tests.support.apps.shop.models import Product

    await _create_tables()
    generator = get_db()
    session = await generator.__anext__()
    session.add(Product(name="Via dependency"))
    with contextlib.suppress(StopAsyncIteration):
        await generator.__anext__()
    assert await _product_names() == ["Via dependency"]
