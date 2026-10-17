from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest
from obox_builder.fields import FieldSpec, field_types
from obox_builder.forms import build_schema
from pydantic import ValidationError


def text_spec(name: str, *, required: bool = False, **kwargs: object) -> FieldSpec:
    return FieldSpec(
        name=name,
        label=name.title(),
        type=field_types.create("text"),
        nullable=not required,
        required=required,
        **kwargs,  # type: ignore[arg-type]
    )


def test_schema_validates_editable_fields_only() -> None:
    specs = [
        text_spec("name", required=True),
        replace(text_spec("token_hash"), options={"secret": True}),
        replace(text_spec("stamp"), readonly=True, editable=False),
    ]
    schema = build_schema(specs, name="Thing", mode="add")
    assert schema.__name__ == "Thing_Add"
    assert set(schema.model_fields) == {"name"}


def test_unknown_keys_are_rejected() -> None:
    schema = build_schema([text_spec("name")])
    with pytest.raises(ValidationError):
        schema.model_validate({"name": "x", "injected": "y"})


def test_required_field_without_default() -> None:
    schema = build_schema([text_spec("name", required=True)])
    with pytest.raises(ValidationError):
        schema.model_validate({})
    assert cast(Any, schema.model_validate({"name": "x"})).name == "x"


def test_coercion_from_raw_strings() -> None:
    specs = [
        FieldSpec(
            name="age",
            label="Age",
            type=field_types.create("integer"),
            nullable=True,
        ),
        FieldSpec(
            name="price",
            label="Price",
            type=field_types.create("decimal"),
            nullable=True,
        ),
    ]
    parsed = cast(Any, build_schema(specs).model_validate({"age": "42", "price": "1.50"}))
    assert parsed.age == 42
    assert str(parsed.price) == "1.50"


def test_nullable_fields_default_to_none() -> None:
    schema = build_schema([text_spec("name")])
    assert cast(Any, schema.model_validate({})).name is None
