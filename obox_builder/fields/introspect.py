"""Derive :class:`FieldSpec` objects from SQLAlchemy models.

Priority (highest first):

1. ``ModelAdmin.fields_config`` override (a ``FieldType`` instance, class or name).
2. ``mapped_column(..., info={'admin': {...}})`` metadata on the model itself.
3. Type inference from the SQLAlchemy column type.
4. Fallback: text, flagged so a system check can warn about it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import sqlalchemy as sa

from obox_builder.fields.base import FieldSpec, FieldType
from obox_builder.fields.registry import field_types
from obox_builder.fields.types.boolean import Checkbox
from obox_builder.fields.types.choice import SingleSelect
from obox_builder.fields.types.json import Json
from obox_builder.fields.types.number import Decimal, Integer
from obox_builder.fields.types.relation import Link, MultiLink
from obox_builder.fields.types.temporal import Date, DateTime, Time
from obox_builder.fields.types.text import LongText, Text
from obox_builder.utils.strings import humanize

RESERVED_INFO_KEYS = {"type", "label", "required", "readonly", "history", "ignore", "secret"}
SECRET_SUFFIXES = ("_hash",)

AdminFieldConfig = Mapping[str, Any]


def introspect_model(
    model: type[Any],
    *,
    overrides: Mapping[str, FieldType | type[FieldType] | str] | None = None,
) -> list[FieldSpec]:
    """Build the field specs for ``model`` in table order."""
    mapper = sa.inspect(model)
    overrides = overrides or {}
    relationship_by_column: dict[str, tuple[str, type[Any]]] = {}
    for rel in mapper.relationships:
        if rel.uselist:
            continue
        for column in rel.local_columns:
            relationship_by_column[column.key] = (rel.key, rel.mapper.class_)

    specs: list[FieldSpec] = []
    for attr in mapper.column_attrs:
        if len(attr.columns) != 1:
            continue
        column = attr.columns[0]
        info = _admin_info(column)
        if info.get("ignore"):
            continue
        override = overrides.get(attr.key)
        field_type, type_options = _resolve_type(column, info, override)
        relationship = relationship_by_column.get(column.key)
        if relationship is not None:
            rel_key, target = relationship
            rel = mapper.relationships[rel_key]
            remote = next(remote for local, remote in rel.local_remote_pairs if local is column)
            field_type = Link(
                **{
                    **type_options,
                    "relationship": rel_key,
                    "target": target,
                    "target_field": remote.key,
                }
            )
            type_options = dict(field_type.options)
        secret = bool(info.get("secret")) or attr.key.endswith(SECRET_SUFFIXES)
        options: dict[str, Any] = dict(type_options)
        if secret:
            options["secret"] = True
        if info.get("unmapped"):
            options["unmapped"] = True
            options["column_type"] = type(column.type).__name__
        label = str(
            info.get("label") or _override_label(override) or _label(attr.key, relationship)
        )
        nullable = bool(column.nullable) if column.nullable is not None else True
        has_default = column.default is not None or column.server_default is not None
        auto_managed = isinstance(getattr(column.server_default, "arg", None), sa.Function)
        readonly = (
            bool(info.get("readonly", False))
            or bool(column.primary_key)
            or isinstance(field_type, Json)
            or auto_managed
        )
        required = (
            bool(info.get("required", False)) or (not nullable and not has_default)
        ) and not readonly
        spec = FieldSpec(
            name=attr.key,
            label=label,
            type=field_type,
            column=column,
            nullable=nullable,
            required=required,
            readonly=readonly,
            editable=not readonly,
            history=bool(info.get("history", True)) and not secret,
            primary_key=bool(column.primary_key),
            help_text=str(info.get("help_text", "")),
            options=options,
        )
        if relationship is not None:
            spec = replace(spec, options={**spec.options, "value_attr": relationship[0]})
        specs.append(spec)
    for rel in mapper.relationships:
        if rel.secondary is None or not rel.uselist:
            continue
        info = dict(rel.info.get("admin", {}))
        if info.get("ignore"):
            continue
        secondary_columns = set(rel.secondary.c)
        link_columns = {column for column in secondary_columns if column.foreign_keys}
        readonly = rel.viewonly or secondary_columns != link_columns or bool(info.get("readonly"))
        field_type = _coerce_override(overrides[rel.key]) if rel.key in overrides else MultiLink()
        specs.append(
            FieldSpec(
                name=rel.key,
                label=str(info.get("label") or humanize(rel.key)),
                type=field_type,
                readonly=readonly,
                editable=not readonly,
                required=bool(info.get("required")),
                help_text=str(info.get("help_text", "")),
                history=False,
                options={
                    **field_type.options,
                    "target": rel.mapper.class_,
                    "relationship": rel.key,
                    "value_attr": rel.key,
                    "secret": bool(info.get("secret")),
                },
            )
        )
    return specs


def _label(key: str, relationship: tuple[str, type[Any]] | None) -> str:
    if relationship is not None:
        return humanize(relationship[0])
    return humanize(key)


def _override_label(override: FieldType | type[FieldType] | str | None) -> str | None:
    if isinstance(override, FieldType):
        label = override.options.get("label")
        return str(label) if label else None
    return None


def _admin_info(column: sa.Column[Any]) -> dict[str, Any]:
    raw = getattr(column, "info", None) or {}
    info = raw.get("admin", {})
    return dict(info) if isinstance(info, Mapping) else {}


def _resolve_type(
    column: sa.Column[Any],
    info: Mapping[str, Any],
    override: FieldType | type[FieldType] | str | None,
) -> tuple[FieldType, Mapping[str, Any]]:
    if override is not None:
        field_type = _coerce_override(override)
        return field_type, field_type.options
    type_name = info.get("type")
    if type_name:
        options = {key: value for key, value in info.items() if key not in RESERVED_INFO_KEYS}
        field_type = field_types.create(str(type_name), **options)
        return field_type, field_type.options
    return _infer(column)


def _coerce_override(override: FieldType | type[FieldType] | str) -> FieldType:
    if isinstance(override, FieldType):
        return override
    if isinstance(override, str):
        return field_types.create(override)
    return override()


def _infer(column: sa.Column[Any]) -> tuple[FieldType, Mapping[str, Any]]:
    column_type = column.type
    if isinstance(column_type, sa.Enum):
        choices = [
            (str(value), str(value).replace("_", " ").title()) for value in column_type.enums
        ]
        return SingleSelect(choices=choices), {"choices": choices}
    if isinstance(column_type, sa.Boolean):
        return Checkbox(), {}
    if isinstance(column_type, sa.DateTime):
        return DateTime(), {}
    if isinstance(column_type, sa.Date):
        return Date(), {}
    if isinstance(column_type, sa.Time):
        return Time(), {}
    if isinstance(column_type, sa.Integer):
        return Integer(), {}
    if isinstance(column_type, sa.Numeric):
        return Decimal(), {}
    if isinstance(column_type, sa.JSON):
        return Json(), {}
    if isinstance(column_type, sa.Text):
        return LongText(), {}
    if isinstance(column_type, (sa.String, sa.Unicode)):
        return Text(), {}
    return Text(), {"unmapped": True, "column_type": type(column_type).__name__}
