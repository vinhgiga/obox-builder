"""Bound forms: raw request data validated per field, with per-field errors."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ValidationError

from obox_builder.fields.base import FieldSpec, RecordContext

__all__ = ["BoundForm", "render_values"]


@dataclass
class BoundForm:
    """Validation result: cleaned values plus errors keyed by field name."""

    specs: Sequence[FieldSpec]
    schema: type[BaseModel]
    cleaned: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, list[str]] = field(default_factory=dict)
    parsed: BaseModel | None = None

    @property
    def is_valid(self) -> bool:
        return self.parsed is not None and not self.errors

    @classmethod
    def bind(
        cls,
        specs: Sequence[FieldSpec],
        schema: type[BaseModel],
        data: Mapping[str, Any],
        *,
        base: RecordContext | None = None,
    ) -> BoundForm:
        base = base or RecordContext()
        ctx = RecordContext(
            obj=base.obj,
            values=dict(data),
            user=base.user,
            extra=base.extra,
        )
        bound = cls(specs=specs, schema=schema)
        payload: dict[str, Any] = {}
        for spec in specs:
            if not spec.is_editable():
                continue
            if (
                spec.type.name == "multi_link"
                and spec.name not in data
                and not spec.required
                and spec.required_if is None
            ):
                continue
            if spec.editable_if is not None and not spec.editable_if(ctx):
                continue
            value = spec.type.from_input(data.get(spec.name), spec)
            required = spec.required or (
                spec.required_if is not None and bool(spec.required_if(ctx))
            )
            if required and (value in (None, "") or value == []):
                bound.errors.setdefault(spec.name, []).append("This field is required.")
                continue
            message = spec.type.validate(value, spec)
            if message:
                bound.errors.setdefault(spec.name, []).append(message)
                continue
            payload[spec.name] = value
        try:
            parsed = schema.model_validate(payload)
        except ValidationError as exc:
            _collect_pydantic_errors(bound, exc)
            return bound
        bound.parsed = parsed
        if ctx.values != payload:
            ctx = RecordContext(obj=ctx.obj, values=payload, user=ctx.user, extra=ctx.extra)
        for spec in specs:
            if spec.valid_if is None or spec.name in bound.errors:
                continue
            message = spec.valid_if(getattr(parsed, spec.name, None), ctx)
            if message:
                bound.errors.setdefault(spec.name, []).append(str(message))
        if bound.errors:
            bound.parsed = None
        bound.cleaned = {name: getattr(parsed, name) for name in payload}
        return bound

    def apply_to(self, obj: Any) -> Any:
        """Assign cleaned values onto a model instance (skips non-editable specs)."""
        by_name = {spec.name: spec for spec in self.specs}
        editable = {spec.name for spec in self.specs if spec.is_editable()}
        for name, value in self.cleaned.items():
            if name not in editable or by_name[name].type.name == "multi_link":
                continue
            if not hasattr(obj, name):
                continue
            if value is None and _uses_column_default(by_name[name]):
                continue
            setattr(obj, name, value)
        return obj


def _uses_column_default(spec: FieldSpec) -> bool:
    """True when ``None`` should be left to a NOT NULL column's own default."""
    column = spec.column
    if column is None or column.nullable:
        return False
    return column.default is not None or column.server_default is not None


def _collect_pydantic_errors(bound: BoundForm, exc: ValidationError) -> None:
    for error in exc.errors():
        loc = error.get("loc") or ()
        field_name = str(loc[0]) if loc else "__all__"
        if field_name in bound.errors:
            continue
        bound.errors.setdefault(field_name, []).append(str(error.get("msg", "Invalid value.")))


def render_values(obj: Any, specs: Sequence[FieldSpec]) -> dict[str, Any]:
    """Values for rendering a form: link specs read the foreign key column."""
    values: dict[str, Any] = {}
    for spec in specs:
        if not spec.is_editable():
            continue
        if spec.type.name == "multi_link":
            target_pk = sa.inspect(spec.options["target"]).primary_key[0].key
            values[spec.name] = [str(getattr(item, target_pk)) for item in getattr(obj, spec.name)]
            continue
        if spec.type.name == "link" and spec.column is not None:
            attr = str(spec.column.key)
        else:
            attr = str(spec.options.get("value_attr") or spec.name)
        values[spec.name] = spec.type.to_input(getattr(obj, attr, None), spec)
    return values
