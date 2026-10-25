"""Model and inline uniqueness checks before database writes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm.exc import UnmappedColumnError

from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.inlines import InlineModelAdmin, InlineRow
from obox_builder.forms import BoundForm


def unique_column_sets(model: type[Any]) -> list[tuple[tuple[Any, ...], bool]]:
    """Return simple column-based unique constraints declared on a mapper."""
    mapper = sa.inspect(model)
    result: list[tuple[tuple[Any, ...], bool]] = []
    seen: set[frozenset[Any]] = set()
    for table in mapper.tables:
        candidates: list[tuple[tuple[Any, ...], bool]] = [
            ((column,), False) for column in table.columns if column.unique
        ]
        candidates.extend(
            (tuple(constraint.columns), bool(getattr(constraint, "nulls_not_distinct", False)))
            for constraint in table.constraints
            if isinstance(constraint, sa.UniqueConstraint)
        )
        candidates.extend(
            (tuple(index.columns), False)
            for index in table.indexes
            if index.unique
            and not any(
                options.get("where") is not None for options in index.dialect_options.values()
            )
            and all(isinstance(column, sa.Column) for column in index.columns)
        )
        for columns, nulls_not_distinct in candidates:
            if not columns or all(column.primary_key for column in columns):
                continue
            signature = frozenset(columns)
            if signature in seen:
                continue
            seen.add(signature)
            result.append((columns, nulls_not_distinct))
    return result


def _attribute_for_column(model: type[Any], column: Any) -> str:
    mapper = sa.inspect(model)
    try:
        return mapper.get_property_by_column(column).key
    except UnmappedColumnError:
        return str(column.key)


def _candidate_values(
    model: type[Any],
    obj: Any,
    bound: BoundForm,
    overrides: Mapping[str, Any],
    columns: Sequence[Any],
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for column in columns:
        name = _attribute_for_column(model, column)
        if name in overrides:
            values[name] = overrides[name]
        elif name in bound.cleaned:
            values[name] = bound.cleaned[name]
        elif obj is not None:
            value = getattr(obj, name, None)
            default = column.default
            values[name] = default.arg if value is None and default and default.is_scalar else value
        else:
            default = column.default
            values[name] = default.arg if default is not None and default.is_scalar else None
    return values


def _form_fields(bound: BoundForm, overrides: Mapping[str, Any]) -> set[str]:
    return set(bound.cleaned) | set(overrides)


def _add_unique_error(bound: BoundForm, model: type[Any], columns: Sequence[Any]) -> None:
    names = [_attribute_for_column(model, column) for column in columns]
    editable_names = [name for name in names if name in bound.cleaned]
    key = editable_names[0] if len(editable_names) == 1 else "__all__"
    message = (
        "This value must be unique."
        if key != "__all__"
        else "These values must be unique together."
    )
    if message not in bound.errors.setdefault(key, []):
        bound.errors[key].append(message)
    bound.parsed = None


async def validate_model_uniqueness(
    ctx: AdminContext,
    model: type[Any],
    bound: BoundForm,
    obj: Any = None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> None:
    """Check declared unique columns against persisted rows, excluding this object."""
    overrides = overrides or {}
    mapper = sa.inspect(model)
    for columns, nulls_not_distinct in unique_column_sets(model):
        names = tuple(_attribute_for_column(model, column) for column in columns)
        if not set(names) <= _form_fields(bound, overrides):
            continue
        values = _candidate_values(model, obj, bound, overrides, columns)
        candidate = tuple(values[name] for name in names)
        if any(value is None for value in candidate) and not nulls_not_distinct:
            continue
        predicates = [
            column.is_(None) if value is None else column == value
            for column, value in zip(columns, candidate, strict=True)
        ]
        if obj is not None:
            for primary_key in mapper.primary_key:
                key = _attribute_for_column(model, primary_key)
                value = getattr(obj, key, None)
                if value is not None:
                    predicates.append(primary_key != value)
        exists = await ctx.db.scalar(
            sa.select(sa.literal(True)).select_from(model).where(*predicates).limit(1)
        )
        if exists:
            _add_unique_error(bound, model, columns)


def _hashable(value: Any) -> Any:
    if hasattr(value, "__mapper__"):
        return sa.inspect(value).identity or id(value)
    try:
        hash(value)
    except TypeError:
        if isinstance(value, Mapping):
            return tuple(sorted((key, _hashable(item)) for key, item in value.items()))
        if isinstance(value, Sequence) and not isinstance(value, str | bytes):
            return tuple(_hashable(item) for item in value)
        return repr(value)
    return value


async def validate_inline_uniqueness(
    ctx: AdminContext,
    inline: InlineModelAdmin,
    rows: Sequence[InlineRow],
    parent: Any,
) -> None:
    """Validate existing-row conflicts and duplicate values within one inline formset."""
    parent_pk = sa.inspect(type(parent)).primary_key[0] if parent is not None else None
    overrides = {
        inline.fk_column.key: getattr(parent, parent_pk.key)
        if parent is not None and parent_pk is not None
        else None
    }
    signature_overrides = overrides.copy()
    if parent is None:
        signature_overrides[inline.fk_column.key] = object()
    candidates: list[tuple[InlineRow, tuple[Any, ...], tuple[Any, ...]]] = []
    for row in rows:
        if row.delete or row.bound is None or not row.bound.is_valid:
            continue
        await validate_model_uniqueness(ctx, inline.model, row.bound, row.obj, overrides=overrides)
        if not row.bound.is_valid:
            continue
        for columns, nulls_not_distinct in unique_column_sets(inline.model):
            names = tuple(_attribute_for_column(inline.model, column) for column in columns)
            if not set(names) <= _form_fields(row.bound, overrides):
                continue
            values = _candidate_values(
                inline.model, row.obj, row.bound, signature_overrides, columns
            )
            signature = tuple(_hashable(values[name]) for name in names)
            if any(value is None for value in signature) and not nulls_not_distinct:
                continue
            candidates.append((row, columns, signature))

    seen: dict[tuple[tuple[Any, ...], tuple[Any, ...]], InlineRow] = {}
    for row, columns, signature in candidates:
        key = (tuple(columns), signature)
        previous = seen.get(key)
        if previous is not None and row.bound is not None:
            _add_unique_error(row.bound, inline.model, columns)
        else:
            seen[key] = row
