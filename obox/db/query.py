"""SQL query composition from transport-independent list controls."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from math import isfinite
from typing import Any, TypedDict

import sqlalchemy as sa

from obox.core.exceptions import BuilderValidationError, InvalidQuery
from obox.db.fields import ModelField, model_value, normalize, validate_text
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


def group_query(query: Any, key: Any, model: type[Any], terms: Sequence[OrderTerm]) -> Any:
    identities = query.order_by(None).with_only_columns(key, maintain_column_froms=True)
    columns = [getattr(model, term["field"]) for term in terms]
    return (
        sa.select(*columns, sa.func.count())
        .where(key.in_(identities))
        .group_by(*columns)
        .order_by(*order_columns(model, terms))
    )


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
    if isinstance(type_, sa.Uuid):
        return "uuid"
    return "text"


def filter_operators(kind: str) -> list[str]:
    if kind == "text":
        return ["contains", "does_not_contain", "is", "is_not", "in", "is_empty", "is_not_empty"]
    if kind in {"number", "date", "datetime", "time"}:
        return ["is", "is_not", "gt", "gte", "lt", "lte", "in", "is_empty", "is_not_empty"]
    return ["is", "is_not", "in", "is_empty", "is_not_empty"]


def _collection_filter(
    model: type[Any], tree: FilterGroup | None, fields: Mapping[str, ModelField]
) -> tuple[dict[str, Any], Any | None]:
    if tree is None:
        return FilterGroup().to_dict(), None

    def compile_term(term: FilterTerm) -> Any | None:
        name, op, raw = term.field, term.op, term.value
        if (spec := fields.get(name)) is None:
            raise InvalidQuery("Invalid filter field.")
        column = spec.column
        if "obox_files" in column.info:
            empty = ~getattr(model, name).any()
            if op not in {"is_empty", "is_not_empty"}:
                raise InvalidQuery("Invalid filter operator.")
            return empty if op == "is_empty" else ~empty
        column = getattr(model, name)
        kind = field_kind(column)
        if op not in filter_operators(kind):
            raise InvalidQuery("Invalid filter operator.")
        if op in {"is_empty", "is_not_empty"}:
            empty = column.is_(None)
            if isinstance(column.type, sa.String) and not isinstance(column.type, sa.Enum):
                empty = sa.or_(empty, column == "")
            return empty if op == "is_empty" else sa.not_(empty)
        if op != "in" and (raw is None or (isinstance(raw, str) and not raw.strip())):
            return None

        def parse_value(raw: Any) -> Any:
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
                if column.type.timezone and value.tzinfo is None:
                    value = value.replace(tzinfo=UTC)
            elif kind == "time":
                value = time.fromisoformat(str(raw))
                if column.type.timezone and value.tzinfo is None:
                    value = value.replace(tzinfo=UTC)
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
            return model_value(column, value)

        try:
            if op == "in":
                if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
                    raise ValueError
                return column.in_([parse_value(item) for item in raw])
            value = parse_value(raw)
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

    clean, expression, _ = compile_filter(tree, compile_term)
    return (clean if isinstance(clean, FilterGroup) else FilterGroup()).to_dict(), expression


class OrderTerm(TypedDict):
    field: str
    descending: bool


def collection_fields(
    model: type[Any],
    readable: Mapping[str, ModelField],
    configured: Sequence[str] | None,
    operation: str,
) -> tuple[str, ...]:
    available = {}
    for name, spec in readable.items():
        column_type = spec.column.type
        if "obox_files" in spec.column.info:
            supported = operation == "filter"
        elif isinstance(column_type, sa.JSON):
            supported = False
        elif operation == "search":
            supported = isinstance(column_type, sa.String) and not isinstance(column_type, sa.Enum)
        elif operation == "group":
            supported = isinstance(
                column_type, sa.Boolean | sa.Integer | sa.Numeric | sa.String | sa.Date
            )
        else:
            supported = operation == "filter" or not isinstance(column_type, sa.Text)
        if supported:
            available[name] = spec
    return tuple(available) if configured is None else normalize(configured, model, available)


@dataclass(frozen=True)
class CollectionQuery:
    filter: dict[str, Any]
    predicate: Any
    sort: list[OrderTerm]
    group: list[OrderTerm]
    search: str
    override_sort: bool

    @classmethod
    def parse(
        cls,
        model: type[Any],
        params: Mapping[str, str],
        *,
        allowed: Mapping[str, tuple[str, ...]],
        specs: Mapping[str, ModelField],
        default_filter: FilterGroup | None = None,
        default_sort: Sequence[str] = (),
        default_group: Sequence[str] = (),
    ) -> CollectionQuery:
        try:
            tree = parse_filter(params["filter"]) if "filter" in params else default_filter
            clean, predicate = _collection_filter(
                model, tree, {name: specs[name] for name in allowed["filter"]}
            )
            search = validate_text(params.get("q", ""))
        except (BuilderValidationError, TypeError, ValueError) as exc:
            raise InvalidQuery("Invalid collection query.") from exc
        if len(search) > 256 or (
            search and (not allowed["search"] or len(search.split(maxsplit=10)) > 10)
        ):
            raise InvalidQuery("Invalid search query.")
        return cls(
            clean,
            predicate,
            parse_order(
                params.get("ordering", ",".join(default_sort)), set(allowed["ordering"]), maximum=5
            ),
            parse_order(
                params.get("group", ",".join(default_group)), set(allowed["group"]), maximum=3
            ),
            search.strip(),
            "ordering" in params or bool(default_sort),
        )

    def apply(self, stmt: Any, model: type[Any], search_fields: Sequence[str]) -> Any:
        if self.predicate is not None:
            stmt = stmt.where(self.predicate)
        if self.search:
            stmt = search_query(stmt, model, search_fields, self.search)
        if self.override_sort or self.group:
            terms = {term["field"]: term for term in self.sort}
            groups = {term["field"]: term for term in self.group}
            ordering = order_columns(
                model,
                [*groups.values(), *(term for name, term in terms.items() if name not in groups)],
            )
            if not self.override_sort:
                ordering.extend(stmt._order_by_clauses)
            stmt = stmt.order_by(None).order_by(*ordering)
        return stmt


def order_columns(model: type[Any], terms: Sequence[OrderTerm]) -> list[Any]:
    return [
        (column.desc() if term["descending"] else column.asc()).nulls_last()
        for term in terms
        for column in (getattr(model, term["field"]),)
    ]


def parse_order(value: str, allowed: set[str], *, maximum: int) -> list[OrderTerm]:
    if len(value) > 1024:
        raise InvalidQuery("Invalid field ordering.")
    result: list[OrderTerm] = []
    seen: set[str] = set()
    for raw in value.split(",") if value else ():
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
