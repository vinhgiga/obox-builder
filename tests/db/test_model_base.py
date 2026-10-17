from __future__ import annotations

from typing import cast

import pytest
import sqlalchemy as sa
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.db.base import Model, camel_to_snake
from sqlalchemy.orm import Mapped, mapped_column


def test_camel_to_snake() -> None:
    assert camel_to_snake("Product") == "product"
    assert camel_to_snake("HTTPResponseLog") == "http_response_log"
    assert camel_to_snake("Order2Item") == "order2_item"
    assert camel_to_snake("APIKey") == "api_key"


@pytest.fixture
def populated(monkeypatch: pytest.MonkeyPatch) -> None:
    import obox_builder
    from obox_builder.conf import SETTINGS_MODULE_ENV

    monkeypatch.setenv(SETTINGS_MODULE_ENV, "tests.support.db_settings")
    obox_builder.setup()


def test_table_names_follow_app_label_and_snake_case(populated: None) -> None:
    from tests.support.apps.shop.models import Category, Order, Product

    assert Product.__tablename__ == "shop_product"
    assert Category.__tablename__ == "shop_category"
    assert Order.__tablename__ == "shop_order"
    assert Product.app_label == "shop"


def test_models_are_registered_in_the_app_registry(populated: None) -> None:
    from obox_builder.apps import apps

    models = {model.__name__: model for model in apps.get_models()}
    assert set(models) == {"Category", "Order", "Product"}
    assert models["Product"] is not None


def test_bare_tables_are_registered(populated: None) -> None:
    from obox_builder.apps import apps

    assert {table.name for table in apps.get_tables()} == {"shop_order_items"}


def test_relationship_defaults_to_lazy_raise(populated: None) -> None:
    from tests.support.apps.shop.models import Product

    relationship = Product.__mapper__.relationships["category"]
    assert relationship.lazy == "raise"


def test_model_outside_installed_app_raises(populated: None) -> None:
    with pytest.raises(ImproperlyConfigured, match="not part of any installed app"):

        class Stray(Model):
            __module__ = "stray_module"


def test_model_can_override_app_label(populated: None) -> None:
    from obox_builder.db.base import ModelBase

    class Special(Model):
        app_label = "custom"

        id: Mapped[int] = mapped_column(primary_key=True)

    try:
        assert Special.__tablename__ == "custom_special"
    finally:
        ModelBase.metadata.remove(cast(sa.Table, Special.__table__))
