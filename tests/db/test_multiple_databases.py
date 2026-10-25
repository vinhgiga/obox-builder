# Adapted from Django Software Foundation's tests/multiple_database/tests.py.
# Copyright (c) Django Software Foundation and individual contributors.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this list of
#    conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice, this list
#    of conditions and the following disclaimer in the documentation and/or other materials
#    provided with the distribution.
# 3. Neither the name of the Django Software Foundation nor the names of its contributors
#    may be used to endorse or promote products derived from this software without specific
#    prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY
# EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL
# THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT
# OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR
# TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import obox_builder
import pytest
import sqlalchemy as sa
from obox_builder.conf import BaseSettings
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.db import get_engine, get_sessionmaker, session_scope
from obox_builder.db.base import ModelBase
from obox_builder.db.router import ConnectionRouter
from pydantic import SecretStr
from sqlalchemy.exc import InvalidRequestError


@pytest.fixture
async def multi_db(tmp_path: Path) -> AsyncIterator[None]:
    from obox_builder.db import dispose

    obox_builder._reset()
    config = BaseSettings(
        SECRET_KEY=SecretStr("m" * 40),
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'default.sqlite3'}",
        DATABASES={"analytics": f"sqlite+aiosqlite:///{tmp_path / 'analytics.sqlite3'}"},
        DATABASE_ROUTERS=["tests.support.database_routers.ShopRouter"],
        INSTALLED_APPS=["tests.support.apps.shop", "obox_builder.contrib.auth"],
    )
    obox_builder.configure(config)
    obox_builder.setup()
    for alias in config.database_urls:
        async with get_engine(alias).begin() as connection:
            await connection.run_sync(ModelBase.metadata.create_all)
    try:
        yield
    finally:
        await dispose()


async def _names(using: str) -> list[str]:
    from tests.support.apps.shop.models import Product

    async with get_sessionmaker(using)() as session:
        return list(await session.scalars(sa.select(Product.name).order_by(Product.name)))


async def test_explicit_database_sessions_are_isolated(multi_db: None) -> None:
    from tests.support.apps.shop.models import Product

    async with session_scope("default") as session:
        session.add(Product(name="default row"))
    async with session_scope("analytics") as session:
        session.add(Product(name="analytics row"))

    assert await _names("default") == ["default row"]
    assert await _names("analytics") == ["analytics row"]
    assert get_engine() is get_engine("default")


async def test_model_router_selects_read_and_write_database(multi_db: None) -> None:
    from tests.support.apps.shop.models import Product

    async with session_scope() as session:
        session.add(Product(name="routed row"))

    assert await _names("default") == []
    assert await _names("analytics") == ["routed row"]
    async with get_sessionmaker()() as session:
        assert list(await session.scalars(sa.select(Product.name))) == ["routed row"]


async def test_read_and_write_routers_can_select_different_databases(multi_db: None) -> None:
    from obox_builder.conf import settings

    from tests.support.apps.shop.models import Product

    settings.DATABASE_ROUTERS[:] = ["tests.support.database_routers.SplitReadWriteRouter"]
    async with session_scope() as session:
        session.add(Product(name="primary row"))

    assert await _names("default") == ["primary row"]
    assert await _names("analytics") == []
    async with get_sessionmaker()() as session:
        assert list(await session.scalars(sa.select(Product.name))) == []


async def test_model_relationships_require_a_shared_database(multi_db: None) -> None:
    from obox_builder.conf import settings

    from tests.support.apps.shop.models import Category, Product

    settings.DATABASE_ROUTERS[:] = ["tests.support.database_routers.SplitShopRouter"]
    async with get_sessionmaker()() as session:
        category = Category(name="local")
        product = Product(name="remote", category=category)
        session.add_all([category, product])
        with pytest.raises(InvalidRequestError, match="Cannot relate"):
            await session.flush()
        await session.rollback()


async def test_foreign_key_and_many_to_many_relations_work_on_one_database(
    multi_db: None,
) -> None:
    from tests.support.apps.shop.models import Category, Label, Order, Product

    async with session_scope() as session:
        category = Category(name="books")
        product = Product(name="guide", category=category)
        order = Order(reference="order-1", labels=[Label(id="priority", title="Priority")])
        session.add_all([category, product, order])

    async with get_sessionmaker()() as session:
        assert (
            await session.scalar(
                sa.select(Product.name).join(Product.category).where(Category.name == "books")
            )
            == "guide"
        )
        assert (
            await session.scalar(
                sa.select(Order.reference).join(Order.labels).where(Label.id == "priority")
            )
            == "order-1"
        )


async def test_session_commits_models_to_each_routed_database(multi_db: None) -> None:
    from obox_builder.contrib.auth.models import User

    from tests.support.apps.shop.models import Product

    async with session_scope() as session:
        session.add_all([Product(name="shop row"), User(username="auth row")])

    assert await _names("analytics") == ["shop row"]
    async with get_sessionmaker("default")() as session:
        assert await session.scalar(sa.select(User.username)) == "auth row"


async def test_subqueries_and_eager_relations_stay_with_the_model_database(
    multi_db: None,
) -> None:
    from sqlalchemy.orm import selectinload

    from tests.support.apps.shop.models import Category, Product

    async with session_scope() as session:
        category = Category(name="category")
        session.add(Product(name="subquery row", category=category))

    async with get_sessionmaker()() as session:
        nested = sa.select(Product.id).where(Product.name == "subquery row")
        assert (
            await session.scalar(sa.select(Product.name).where(Product.id.in_(nested)))
            == "subquery row"
        )
        product = await session.scalar(sa.select(Product).options(selectinload(Product.category)))
        assert product is not None
        assert product.category is not None
        assert product.category.name == "category"


async def test_bulk_update_delete_and_refresh_follow_model_router(multi_db: None) -> None:
    from tests.support.apps.shop.models import Product

    async with session_scope() as session:
        product = Product(name="before")
        session.add(product)
        await session.flush()
        product_id = product.id

    async with get_sessionmaker()() as session:
        loaded_product = await session.get(Product, product_id)
        assert loaded_product is not None
        await session.execute(
            sa.update(Product).where(Product.id == product_id).values(name="after")
        )
        await session.refresh(loaded_product)
        assert loaded_product.name == "after"
        await session.execute(sa.delete(Product).where(Product.id == product_id))
        await session.commit()

    assert await _names("analytics") == []


async def test_raw_sql_uses_an_explicit_alias_session(multi_db: None) -> None:
    from tests.support.apps.shop.models import Product

    async with session_scope("analytics") as session:
        session.add(Product(name="raw target"))
    async with get_sessionmaker("analytics")() as session:
        result = await session.execute(sa.text("SELECT name FROM shop_product"))
        assert result.scalar_one() == "raw target"


async def test_orm_join_cannot_span_routed_databases(multi_db: None) -> None:
    from obox_builder.conf import settings

    from tests.support.apps.shop.models import Category, Product

    settings.DATABASE_ROUTERS[:] = ["tests.support.database_routers.SplitShopRouter"]
    async with get_sessionmaker()() as session:
        statement = sa.select(Product).join(Category)
        with pytest.raises(InvalidRequestError, match="cannot span multiple"):
            await session.execute(statement)


async def test_many_to_many_cannot_cross_routed_databases(multi_db: None) -> None:
    from obox_builder.conf import settings

    from tests.support.apps.shop.models import Label, Order

    settings.DATABASE_ROUTERS[:] = ["tests.support.database_routers.SplitManyRouter"]
    async with get_sessionmaker()() as session:
        order = Order(reference="split", labels=[Label(id="other", title="Other")])
        session.add(order)
        with pytest.raises(InvalidRequestError, match="Cannot relate"):
            await session.flush()
        await session.rollback()


def test_unknown_alias_is_rejected(multi_db: None) -> None:
    with pytest.raises(ImproperlyConfigured, match="Unknown database alias"):
        get_sessionmaker("missing")


async def test_explicit_sessionmaker_pins_router_to_requested_alias(multi_db: None) -> None:
    from obox_builder.conf import settings

    from tests.support.apps.shop.models import Product

    settings.DATABASE_ROUTERS[:] = ["tests.support.database_routers.SplitShopRouter"]
    async with get_sessionmaker("default")() as session:
        session.add(Product(name="pinned default"))
        await session.commit()
    async with get_sessionmaker("default")() as session:
        assert list(await session.scalars(sa.select(Product.name))) == ["pinned default"]


async def test_auth_models_follow_the_configured_router(multi_db: None) -> None:
    from obox_builder.conf import settings
    from obox_builder.contrib.auth.models import User

    settings.DATABASE_ROUTERS[:] = ["tests.support.database_routers.AuthRouter"]
    async with session_scope() as session:
        session.add(User(username="on-analytics"))

    async with get_sessionmaker("default")() as session:
        assert await session.scalar(sa.select(User.id)) is None
    async with get_sessionmaker()() as session:
        assert await session.scalar(sa.select(User.username)) == "on-analytics"


def test_router_order_uses_first_non_null_decision(multi_db: None) -> None:
    from tests.support.apps.shop.models import Product

    class ReadRouter:
        def db_for_read(self, model: type[object], **hints: object) -> str:
            return "analytics"

    class WriteRouter:
        def db_for_write(self, model: type[object], **hints: object) -> str:
            return "default"

    selected = ConnectionRouter([ReadRouter(), WriteRouter()])
    assert selected.db_for_read(Product) == "analytics"
    assert selected.db_for_write(Product) == "default"


def test_router_rejects_unknown_alias_from_router(multi_db: None) -> None:
    from tests.support.apps.shop.models import Product

    with pytest.raises(ImproperlyConfigured, match="unknown alias"):
        ConnectionRouter(["tests.support.database_routers.InvalidRouter"]).db_for_read(Product)


def test_database_router_system_check_reports_unknown_alias(multi_db: None) -> None:
    from obox_builder.conf import settings
    from obox_builder.core.checks.builtin import check_database_routers

    settings.DATABASE_ROUTERS[:] = ["tests.support.database_routers.SplitShopRouter"]
    assert check_database_routers() == []
    settings.DATABASE_ROUTERS[:] = ["tests.support.database_routers.InvalidRouter"]
    messages = check_database_routers()
    assert len(messages) == 1
    assert messages[0].id == "obox.E005"
