"""SQL query composition from transport-independent list controls."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime, time
from decimal import Decimal
from math import isfinite
from typing import Any, TypedDict

import sqlalchemy as sa

from obox.core.exceptions import BuilderValidationError, InvalidQuery
from obox.db.fields import model_value, validate_text
from obox.db.filters import FilterGroup, FilterTerm, compile_filter, parse_filter


def scope_query(query: Any, model: type[Any]) -> Any:
    """Preserve a limited selection before applying additional conditions."""
    if not query._has_row_limiting_clause:
        return query
    pk = sa.inspect(model).primary_key[0]
    identities = query.with_only_columns(pk, maintain_column_froms=True)
    return (
        sa.select(model)
        .where(pk.in_(identities))
        .options(*query._with_options)
        .order_by(*query._order_by_clauses)
    )


def group_query(query: Any, key: Any, columns: Sequence[Any], *, order: str) -> Any:
    """Count model records in each group of the scoped selection."""
    identities = query.order_by(None).with_only_columns(key, maintain_column_froms=True)
    count = sa.func.count()
    sort, _, direction = order.partition(".")
    keys = [
        (column.desc() if sort == "key" and direction == "desc" else column.asc()).nulls_last()
        for column in columns
    ]
    if sort == "count":
        keys.insert(0, count.desc() if direction == "desc" else count.asc())
    return sa.select(*columns, count).where(key.in_(identities)).group_by(*columns).order_by(*keys)


def model_column(model: type[Any], name: str) -> Any | None:
    """Return a mapped scalar column for a directly addressable model field."""
    attribute = getattr(model, name, None)
    property_ = getattr(attribute, "property", None)
    columns = getattr(property_, "columns", None)
    return columns[0] if columns else None


def field_kind(column: Any) -> str:
    type_ = column.type
    if isinstance(type_, sa.Enum):
        return "choice"
    if isinstance(type_, sa.Boolean):
        return "boolean"
    if isinstance(type_, sa.Integer | sa.Numeric | sa.Float):
        return "number"
    if isinstance(type_, sa.DateTime):
        return "datetime"
    if isinstance(type_, sa.Date):
        return "date"
    if isinstance(type_, sa.Time):
        return "time"
    return "text"


def filter_operators(kind: str) -> list[str]:
    if kind == "text":
        return ["contains", "does_not_contain", "is", "is_not", "is_empty", "is_not_empty"]
    if kind in {"number", "date", "datetime", "time"}:
        return ["is", "is_not", "gt", "gte", "lt", "lte", "is_empty", "is_not_empty"]
    return ["is", "is_not", "is_empty", "is_not_empty"]


def filter_expression(
    model: type[Any],
    data: Any,
    allowed: set[str],
) -> tuple[dict[str, Any], Any | None, int]:
    try:
        return compile_view_filter(model, parse_filter(json.dumps(data)), allowed)
    except (TypeError, ValueError, RecursionError, BuilderValidationError) as exc:
        raise InvalidQuery("Invalid filter query.") from exc


def compile_view_filter(
    model: type[Any], tree: FilterGroup | None, allowed: set[str]
) -> tuple[dict[str, Any], Any | None, int]:
    if tree is None:
        return {"conjunction": "and", "children": []}, None, 0

    def compile_term(term: FilterTerm) -> Any | None:
        name, op, raw = term.field, term.op, term.value
        if name not in allowed:
            raise InvalidQuery("Invalid filter field.")
        column = model_column(model, name)
        if column is None:
            raise InvalidQuery("Invalid filter field.")
        kind = field_kind(column)
        if op not in filter_operators(kind):
            raise InvalidQuery("Invalid filter operator.")
        if op not in {"is_empty", "is_not_empty"} and (raw is None or str(raw).strip() == ""):
            return None
        if op in {"is_empty", "is_not_empty"}:
            empty = column.is_(None)
            if isinstance(column.type, sa.String) and not isinstance(column.type, sa.Enum):
                empty = sa.or_(empty, column == "")
            return empty if op == "is_empty" else sa.not_(empty)
        try:
            if not isinstance(raw, str | int | float | bool):
                raise ValueError
            if kind == "boolean":
                if str(raw).lower() not in {"true", "false", "1", "0"}:
                    raise ValueError
                value: Any = str(raw).lower() in {"true", "1"}
            elif kind == "date":
                value = date.fromisoformat(str(raw))
            elif kind == "datetime":
                value = datetime.fromisoformat(str(raw))
            elif kind == "time":
                value = time.fromisoformat(str(raw))
            elif isinstance(column.type, sa.Enum):
                enum = column.type.enum_class
                if raw in column.type.enums:
                    value = column.type._object_value_for_elem(raw)
                elif enum is not None:
                    value = enum(raw)
                else:
                    raise ValueError
            else:
                python_type = column.type.python_type
                value = python_type(str(raw)) if python_type is not str else validate_text(str(raw))
            if (
                (isinstance(value, float) and not isfinite(value))
                or (isinstance(value, Decimal) and not value.is_finite())
                or (isinstance(value, int) and not -(2**63) <= value < 2**63)
            ):
                raise ValueError
            value = model_value(column, value)
        except (ArithmeticError, AttributeError, NotImplementedError, TypeError, ValueError) as exc:
            raise InvalidQuery("Invalid filter value.") from exc
        if op == "contains":
            return column.icontains(str(raw), autoescape=True)
        if op == "does_not_contain":
            return sa.and_(
                column.is_not(None), sa.not_(column.icontains(str(raw), autoescape=True))
            )
        if op == "is":
            return column == value
        if op == "is_not":
            return sa.and_(column.is_not(None), column != value)
        return {
            "gt": column > value,
            "gte": column >= value,
            "lt": column < value,
            "lte": column <= value,
        }[op]

    clean, expression, count = compile_filter(tree, compile_term)

    def as_dict(node: FilterGroup | FilterTerm) -> dict[str, Any]:
        if isinstance(node, FilterTerm):
            return {
                "field": node.field,
                "op": node.op,
                "value": "" if node.value is None else str(node.value),
            }
        return {
            "conjunction": node.conjunction,
            "children": [as_dict(child) for child in node.children],
        }

    return (
        as_dict(clean) if clean is not None else {"conjunction": tree.conjunction, "children": []},
        expression,
        count,
    )


class OrderTerm(TypedDict):
    field: str
    descending: bool


def parse_order(value: str, allowed: set[str], *, maximum: int) -> list[OrderTerm]:
    result: list[OrderTerm] = []
    seen: set[str] = set()
    for raw in value.split(","):
        if not raw:
            continue
        name = raw[1:] if raw.startswith("-") else raw
        if name not in allowed or name in seen or len(result) >= maximum:
            raise InvalidQuery("Invalid field ordering.")
        seen.add(name)
        result.append({"field": name, "descending": raw.startswith("-")})
    return result


def search_query(
    query: Any, model: type[Any], fields: Sequence[str], term: str, *, split_words: bool = True
) -> Any:
    for word in term.split() if split_words else [term]:
        predicates = []
        for name in fields:
            prefix = name[:1]
            column = getattr(model, name[1:] if prefix in ("^", "=") else name)
            if not isinstance(column.type, sa.String) or isinstance(column.type, sa.Enum):
                column = sa.cast(column, sa.String)
            predicates.append(
                column.istartswith(word, autoescape=True)
                if prefix == "^"
                else sa.func.lower(column) == sa.func.lower(sa.literal(word))
                if prefix == "="
                else column.icontains(word, autoescape=True)
            )
        if predicates:
            query = query.where(sa.or_(*predicates))
    return query
