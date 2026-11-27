from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, Field

from obox.core.exceptions import BuilderValidationError, InvalidQuery
from obox.db.fields import validate_text
from obox.db.filters import MAX_FILTER_NODES, FilterGroup, FilterTerm, parse_filter
from obox.db.query import (
    OrderTerm,
    compile_view_filter,
    filter_expression,
    parse_order,
    scope_query,
    search_query,
)

QUERY_PARAMETERS = frozenset({"filter", "ordering", "group", "q", "page"})


class ViewConfig(BaseModel, frozen=True, extra="forbid"):
    filter: FilterGroup | None = None
    sort: tuple[str, ...] = Field(default=(), max_length=5)
    group: tuple[str, ...] = Field(default=(), max_length=3)


@dataclass(frozen=True)
class ViewQuery:
    filter: dict[str, Any]
    predicate: Any
    sort: list[OrderTerm]
    group: list[OrderTerm]
    search: str
    search_fields: tuple[str, ...]
    override_sort: bool

    @classmethod
    def parse(
        cls,
        model: type[Any],
        params: Mapping[str, str],
        allowed: set[str],
        *,
        defaults: ViewConfig,
        search_fields: Sequence[str] = (),
    ) -> ViewQuery:
        if raw_filter := params.get("filter"):
            try:
                tree = parse_filter(raw_filter)
            except (BuilderValidationError, TypeError) as exc:
                raise InvalidQuery("Invalid filter query.") from exc
            clean, predicate, _ = compile_view_filter(model, tree, allowed)
        else:
            flat = {name.removesuffix("__exact") for name in params if name not in QUERY_PARAMETERS}
            if not flat <= allowed:
                raise InvalidQuery("Invalid filter field.")
            if len(flat) > MAX_FILTER_NODES:
                raise InvalidQuery("Invalid filter query.")
            if not flat and defaults.filter is not None:
                clean, predicate, _ = filter_expression(model, defaults.filter.to_dict(), allowed)
            else:
                tree = FilterGroup(
                    children=tuple(
                        FilterTerm(name, "is", params.get(f"{name}__exact", params.get(name)))
                        for name in sorted(flat)
                    )
                )
                clean, predicate, _ = compile_view_filter(model, tree, allowed)
        searchable = tuple(
            name
            for name in search_fields
            if (name[1:] if name.startswith(("^", "=")) else name) in allowed
        )
        try:
            search = validate_text(params.get("q", "").strip())
        except ValueError as exc:
            raise InvalidQuery("Invalid search query.") from exc
        if search and (not searchable or len(search.split(maxsplit=10)) > 10):
            raise InvalidQuery("Invalid search query.")
        return cls(
            clean,
            predicate,
            parse_order(params.get("ordering", ",".join(defaults.sort)), allowed, maximum=5),
            parse_order(params.get("group", ",".join(defaults.group)), allowed, maximum=3),
            search,
            searchable,
            "ordering" in params or bool(defaults.sort),
        )

    def apply(self, query: Any, model: type[Any]) -> Any:
        query = scope_query(query, model)
        if self.predicate is not None:
            query = query.where(self.predicate)
        query = search_query(query, model, self.search_fields, self.search)
        if self.override_sort or self.group:
            ordering, seen = [], set()
            for term in [*self.group, *self.sort]:
                name = term["field"]
                if name in seen:
                    continue
                seen.add(name)
                column = getattr(model, name)
                order = column.desc() if term["descending"] else column.asc()
                ordering.append(order.nulls_last())
            if not self.override_sort:
                ordering.extend(query._order_by_clauses)
            ordering.extend(sa.inspect(model).primary_key)
            query = query.order_by(None).order_by(*ordering)
        return query
