from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from obox_builder.db import JSONB, Model
from obox_builder.fields import introspect_model
from sqlalchemy.orm import Mapped, mapped_column

from tests.support.apps.shop.models import Category, Product


class Token(Model):
    __tablename__ = "shop_token"
    app_label = "shop"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(sa.String(128))
    note: Mapped[str | None] = mapped_column(
        sa.String(50), info={"admin": {"label": "Note", "secret": True}}
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    blob: Mapped[bytes] = mapped_column(sa.LargeBinary)


def specs_for(model: type[Any]) -> dict[str, Any]:
    return {spec.name: spec for spec in introspect_model(model)}


def test_primary_key_is_readonly() -> None:
    spec = specs_for(Category)["id"]
    assert spec.primary_key is True
    assert spec.readonly is True
    assert spec.editable is False
    assert spec.type.name == "integer"


def test_plain_columns_infer_their_types() -> None:
    specs = specs_for(Product)
    assert specs["name"].type.name == "text"
    assert specs["price_cents"].type.name == "integer"
    assert specs["is_active"].type.name == "checkbox"
    assert specs["status"].type.name == "single_select"


def test_required_comes_from_nullability_and_defaults() -> None:
    specs = specs_for(Product)
    assert specs["name"].required is True
    assert specs["status"].required is True
    assert specs["price_cents"].required is False
    assert specs["category_id"].required is False
    assert specs["created_at"].required is False


def test_info_options_become_type_options_and_flags() -> None:
    status = specs_for(Product)["status"]
    assert status.required is True
    assert status.type.options["choices"] == [("draft", "Draft"), ("live", "Live")]


def test_foreign_key_becomes_link_with_relationship_target() -> None:
    spec = specs_for(Product)["category_id"]
    assert spec.type.name == "link"
    assert spec.options["relationship"] == "category"
    assert spec.options["value_attr"] == "category"
    assert spec.options["target"] is Category
    assert spec.label == "Category"


def test_json_columns_are_editable_with_the_json_widget() -> None:
    meta = specs_for(Product)["meta"]
    assert meta.type.name == "json"
    assert meta.type.widget == "json"
    assert meta.editable is True


def test_secret_by_suffix_and_by_info() -> None:
    specs = specs_for(Token)
    assert specs["token_hash"].secret is True
    assert specs["token_hash"].history is False
    assert specs["note"].secret is True
    assert specs["note"].label == "Note"


def test_unmapped_column_type_falls_back_to_text_with_flag() -> None:
    spec = specs_for(Token)["blob"]
    assert spec.type.name == "text"
    assert spec.options.get("unmapped") is True
    assert spec.options.get("column_type") == "LargeBinary"


def test_overrides_win_over_inference() -> None:
    from obox_builder.fields import field_types

    specs = {
        spec.name: spec
        for spec in introspect_model(Product, overrides={"name": field_types.create("long_text")})
    }
    assert specs["name"].type.name == "long_text"


def test_timestamps_come_from_the_mixin() -> None:
    specs = specs_for(Product)
    assert {"created_at", "updated_at"} <= set(specs)
    assert specs["created_at"].readonly is True
    assert specs["updated_at"].readonly is True


@pytest.fixture(autouse=True, scope="module")
def _cleanup_metadata() -> Any:
    yield
    table = sa.inspect(Token).local_table
    assert isinstance(table, sa.Table)
    Model.metadata.remove(table)
