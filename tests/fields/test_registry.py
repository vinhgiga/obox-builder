from __future__ import annotations

from typing import Any

import pytest
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.fields import FieldType, field_types
from obox_builder.fields.base import Cell, FieldSpec
from obox_builder.fields.registry import FieldTypes


class Shouty(FieldType):
    name = "shouty"

    def annotation(self, spec: FieldSpec) -> Any:
        return str


def test_builtin_types_are_registered() -> None:
    names = field_types.names
    for expected in (
        "text",
        "long_text",
        "email",
        "url",
        "phone",
        "integer",
        "decimal",
        "checkbox",
        "date",
        "datetime",
        "time",
        "single_select",
        "link",
        "json",
    ):
        assert expected in names


def test_create_returns_instance_with_options() -> None:
    instance = field_types.create("single_select", choices=[("a", "A")])
    assert instance.name == "single_select"
    assert instance.options["choices"] == [("a", "A")]


def test_resolve_unknown_raises_lookup_error() -> None:
    with pytest.raises(LookupError, match="Unknown field type"):
        field_types.resolve("nope")


def test_register_duplicate_without_replace_raises() -> None:
    registry = FieldTypes()
    registry.register(Shouty)
    with pytest.raises(ImproperlyConfigured, match="already registered"):
        registry.register(Shouty)
    registry.register(Shouty, replace=True)
    assert registry.resolve("shouty") is Shouty


def test_type_without_name_is_rejected() -> None:
    class Nameless(FieldType):
        name = ""

        def annotation(self, spec: FieldSpec) -> Any:
            return str

    registry = FieldTypes()
    with pytest.raises(ImproperlyConfigured, match="must define a name"):
        registry.register(Nameless)


def test_checkbox_cell_and_input_round_trip() -> None:
    checkbox = field_types.create("checkbox")
    spec = FieldSpec(name="flag", label="Flag", type=checkbox)
    assert checkbox.cell(True, spec) == Cell(text="Yes", value=True, kind="boolean")
    assert checkbox.cell(None, spec).text == "No"
    assert checkbox.to_input(True, spec) == "true"
    assert checkbox.to_input(False, spec) == ""
