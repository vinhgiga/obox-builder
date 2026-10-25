from __future__ import annotations

from dataclasses import replace

from obox_builder.fields import FieldSpec, field_types
from obox_builder.forms import BoundForm, build_schema


class Thing:
    def __init__(self) -> None:
        self.name: str | None = None
        self.age: int | None = None
        self.price: int | None = None
        self.flag: bool = False
        self.locked: str = "original"


def spec(name: str, type_name: str, **kwargs: object) -> FieldSpec:
    return FieldSpec(
        name=name,
        label=name.title(),
        type=field_types.create(type_name),
        nullable=True,
        **kwargs,  # type: ignore[arg-type]
    )


def bind(specs: list[FieldSpec], data: dict[str, object], base: object = None) -> BoundForm:
    schema = build_schema(specs)
    return BoundForm.bind(specs, schema, data, base=base)  # type: ignore[arg-type]


def test_valid_data_is_cleaned_and_applied() -> None:
    specs = [spec("name", "text"), spec("age", "integer"), spec("flag", "checkbox")]
    bound = bind(specs, {"name": " Widget ", "age": "5", "flag": "true"})
    assert bound.is_valid
    assert bound.errors == {}
    target = Thing()
    bound.apply_to(target)
    assert target.name == "Widget"
    assert target.age == 5
    assert target.flag is True


def test_missing_required_field_reports_field_error() -> None:
    specs = [replace(spec("name", "text", required=True), nullable=False)]
    bound = bind(specs, {})
    assert not bound.is_valid
    assert bound.errors["name"] == ["This field is required."]


def test_invalid_value_is_mapped_to_the_field() -> None:
    bound = bind([spec("age", "integer")], {"age": "not-a-number"})
    assert not bound.is_valid
    assert "age" in bound.errors


def test_readonly_and_secret_fields_are_ignored() -> None:
    specs = [
        replace(spec("locked", "text"), readonly=True, editable=False),
        replace(spec("token_hash", "text"), options={"secret": True}),
    ]
    bound = bind(specs, {"locked": "injected", "token_hash": "injected"})
    assert bound.is_valid
    assert bound.cleaned == {}
    target = Thing()
    bound.apply_to(target)
    assert target.locked == "original"


def test_editable_if_false_blocks_assignment() -> None:
    specs = [
        replace(spec("locked", "text"), editable_if=lambda ctx: False),
        spec("name", "text"),
    ]
    bound = bind(specs, {"locked": "injected", "name": "ok"})
    assert bound.is_valid
    assert "locked" not in bound.cleaned
    target = Thing()
    bound.apply_to(target)
    assert target.locked == "original"
    assert target.name == "ok"


def test_required_if_reads_posted_values() -> None:
    specs = [
        spec("name", "text"),
        replace(
            spec("age", "integer"),
            required_if=lambda ctx: ctx.values.get("name") == "person",
        ),
    ]
    bound = bind(specs, {"name": "person"})
    assert not bound.is_valid
    assert bound.errors["age"] == ["This field is required."]
    assert bind(specs, {"name": "robot"}).is_valid


def test_valid_if_hook_reports_business_errors() -> None:
    specs = [
        replace(
            spec("age", "integer"),
            valid_if=lambda value, ctx: "Must be positive." if (value or 0) < 0 else None,
        )
    ]
    bound = bind(specs, {"age": "-3"})
    assert not bound.is_valid
    assert bound.errors["age"] == ["Must be positive."]
    assert bind(specs, {"age": "3"}).is_valid


def test_context_exposes_the_target_object() -> None:
    from obox_builder.fields import RecordContext

    target = Thing()
    seen: list[object] = []

    def remember(value: object, ctx: RecordContext) -> None:
        seen.append(ctx.obj)

    specs = [
        replace(
            spec("name", "text"),
            valid_if=remember,
        )
    ]
    bind(specs, {"name": "x"}, base=RecordContext(obj=target))
    assert seen == [target]


def test_partial_bind_preserves_omitted_required_values_and_conditions() -> None:
    from obox_builder.fields import RecordContext

    target = Thing()
    target.name, target.age = "person", 35
    specs = [
        spec("name", "text", required=True),
        replace(spec("age", "integer"), editable_if=lambda ctx: ctx.values.get("name") == "person"),
    ]
    bound = BoundForm.bind(
        specs, build_schema(specs), {"age": 36}, base=RecordContext(obj=target), partial=True
    )
    assert bound.is_valid
    assert bound.cleaned == {"age": 36}
    bound.apply_to(target)
    assert (target.name, target.age) == ("person", 36)


def test_partial_bind_without_object_only_requires_submitted_fields() -> None:
    specs = [spec("name", "text", required=True), spec("age", "integer")]
    assert BoundForm.bind(specs, build_schema(specs), {"age": 1}, partial=True).is_valid
    invalid = BoundForm.bind(specs, build_schema(specs), {"name": ""}, partial=True)
    assert not invalid.is_valid
    assert "name" in invalid.errors


def test_apply_to_skips_attributes_the_model_lacks() -> None:
    specs = [spec("name", "text"), spec("unknown", "text")]
    bound = bind(specs, {"name": "x", "unknown": "y"})
    assert bound.is_valid
    target = Thing()
    bound.apply_to(target)
    assert target.name == "x"


def test_apply_to_leaves_none_to_not_null_column_defaults() -> None:
    import sqlalchemy as sa

    defaulted = replace(
        spec("price", "integer"),
        column=sa.Column("price", sa.Integer, nullable=False, server_default=sa.text("0")),
    )
    bound = bind([defaulted], {"price": ""})
    assert bound.is_valid
    target = Thing()
    target.price = 99
    bound.apply_to(target)
    assert target.price == 99

    nullable = replace(spec("age", "integer"), column=sa.Column("age", sa.Integer, nullable=True))
    bound = bind([nullable], {"age": ""})
    assert bound.is_valid
    target = Thing()
    bound.apply_to(target)
    assert target.age is None
