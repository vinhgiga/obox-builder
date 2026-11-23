"""SQL query composition from transport-independent list controls."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from typing import Any

import sqlalchemy as sa

from obox.core.domain import InvalidQuery, PermissionDenied


def _column(model: type[Any], name: str) -> Any | None:
    """Return a mapped scalar column for a directly addressable model field."""
    attribute = getattr(model, name, None)
    property_ = getattr(attribute, "property", None)
    columns = getattr(property_, "columns", None)
    return columns[0] if columns else None


def _field_kind(column: Any) -> str:
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


def _filter_operators(kind: str) -> list[str]:
    if kind == "text":
        return ["contains", "does_not_contain", "is", "is_not", "is_empty", "is_not_empty"]
    if kind in {"number", "date", "datetime", "time"}:
        return ["is", "is_not", "gt", "gte", "lt", "lte", "is_empty", "is_not_empty"]
    return ["is", "is_not", "is_empty", "is_not_empty"]


def _filter_expression(
    model: type[Any],
    data: Any,
    allowed: set[str],
    *,
    depth: int = 0,
    budget: list[int] | None = None,
) -> tuple[dict[str, Any], Any | None, int]:
    if budget is None:
        budget = [0]
    if depth > 4 or not isinstance(data, dict):
        raise InvalidQuery("Invalid filter query.")
    conjunction = data.get("conjunction", "and")
    if conjunction not in {"and", "or"}:
        raise InvalidQuery("Invalid filter query.")
    children = data.get("children", [])
    if not isinstance(children, list):
        raise InvalidQuery("Invalid filter query.")
    clean_children: list[dict[str, Any]] = []
    expressions: list[Any] = []
    count = 0
    for child in children:
        budget[0] += 1
        if budget[0] > 50:
            raise InvalidQuery("Too many filter conditions.")
        if isinstance(child, dict) and isinstance(child.get("children"), list):
            clean, expression, nested_count = _filter_expression(
                model, child, allowed, depth=depth + 1, budget=budget
            )
            if nested_count and expression is not None:
                clean_children.append(clean)
                expressions.append(expression)
                count += nested_count
            continue
        if not isinstance(child, dict):
            raise InvalidQuery("Invalid filter query.")
        name, op = child.get("field"), child.get("op", "is")
        if not isinstance(name, str) or name not in allowed or not isinstance(op, str):
            raise InvalidQuery("Invalid filter field.")
        column = _column(model, name)
        if column is None:
            raise InvalidQuery("Invalid filter field.")
        kind = _field_kind(column)
        if op not in _filter_operators(kind):
            raise InvalidQuery("Invalid filter operator.")
        raw = child.get("value", "")
        if op not in {"is_empty", "is_not_empty"} and (raw is None or str(raw).strip() == ""):
            continue
        if op in {"is_empty", "is_not_empty"}:
            empty = column.is_(None)
            if isinstance(column.type, sa.String) and not isinstance(column.type, sa.Enum):
                empty = sa.or_(empty, column == "")
            expression = empty if op == "is_empty" else sa.not_(empty)
        else:
            try:
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
                else:
                    python_type = column.type.python_type
                    value = python_type(raw) if python_type is not str else str(raw)
            except (AttributeError, TypeError, ValueError) as exc:
                raise InvalidQuery("Invalid filter value.") from exc
            if op == "contains":
                expression = column.icontains(str(raw), autoescape=True)
            elif op == "does_not_contain":
                expression = sa.and_(
                    column.is_not(None), sa.not_(column.icontains(str(raw), autoescape=True))
                )
            elif op == "is":
                expression = column == value
            elif op == "is_not":
                expression = sa.and_(column.is_not(None), column != value)
            else:
                expression = {
                    "gt": column > value,
                    "gte": column >= value,
                    "lt": column < value,
                    "lte": column <= value,
                }[op]
        clean_children.append({"field": name, "op": op, "value": str(raw)})
        expressions.append(expression)
        count += 1
    clean_group = {"conjunction": conjunction, "children": clean_children}
    if not expressions:
        return clean_group, None, 0
    return clean_group, (sa.and_ if conjunction == "and" else sa.or_)(*expressions), count


def _parse_order(value: str, allowed: set[str], *, maximum: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
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
            predicates.append(
                column.istartswith(word, autoescape=True)
                if prefix == "^"
                else sa.func.lower(column) == word.lower()
                if prefix == "="
                else column.icontains(word, autoescape=True)
            )
        if predicates:
            query = query.where(sa.or_(*predicates))
    return query


def list_query(
    query: Any,
    model: type[Any],
    params: Mapping[str, str],
    *,
    filters: Sequence[str],
    allowed: set[str],
) -> Any:
    for name in filters:
        raw = params.get(name + "__exact", params.get(name))
        if raw is None:
            continue
        column = getattr(model, name)
        try:
            if isinstance(column.type, sa.Boolean):
                if raw not in ("0", "1", "True", "False"):
                    raise ValueError
                value = raw in ("1", "True")
            else:
                value = column.type.python_type(raw)
        except (TypeError, ValueError) as exc:
            raise PermissionDenied("Invalid filter.") from exc
        query = query.where(column == value)
    if raw_filter := params.get("filter"):
        try:
            data = json.loads(raw_filter)
        except (TypeError, ValueError) as exc:
            raise InvalidQuery("Invalid filter query.") from exc
        _, expression, _ = _filter_expression(model, data, allowed)
        if expression is not None:
            query = query.where(expression)
    groups = _parse_order(params.get("group", ""), allowed, maximum=3)
    if "ordering" in params or groups:
        sorts = _parse_order(params.get("ordering", ""), allowed, maximum=5)
        ordering, seen = [], set()
        for term in [*groups, *sorts]:
            if term["field"] in seen:
                continue
            seen.add(term["field"])
            column = getattr(model, term["field"])
            ordering.append(column.desc() if term["descending"] else column.asc())
        ordering.extend(sa.inspect(model).primary_key)
        query = query.order_by(None).order_by(*ordering)
    return query
