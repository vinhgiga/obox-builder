"""QuerySpec: parse and whitelist list-page query parameters into SQL.

Parsing and validation are pure Python (no database needed); ``apply`` turns the
spec into SQLAlchemy WHERE/ORDER BY clauses. Invalid input raises
:class:`QuerySpecError` so views can answer 400 instead of guessing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import Select, or_

from obox_builder.fields.base import FieldFilterError, FieldSpec

__all__ = ["FilterTerm", "QuerySpec", "QuerySpecError", "SortTerm"]


class QuerySpecError(ValueError):
    """The query string asked for something the field specs do not allow."""


@dataclass(frozen=True)
class FilterTerm:
    field: str
    op: str
    value: Any


@dataclass(frozen=True)
class SortTerm:
    field: str
    descending: bool = False


@dataclass
class QuerySpec:
    filters: list[FilterTerm] = field(default_factory=list)
    sort: list[SortTerm] = field(default_factory=list)
    q: str = ""
    page: int = 1
    size: int = 50

    # -- parsing ------------------------------------------------------------
    @classmethod
    def parse(
        cls,
        params: Any,
        specs: Mapping[str, FieldSpec],
        *,
        default_sort: Sequence[str] = (),
        default_size: int = 50,
        max_size: int = 200,
    ) -> QuerySpec:
        spec = cls(size=_clamp_size(_get(params, "size"), default_size, max_size))
        spec.page = _clamp_page(_get(params, "page"))
        spec.q = (_get(params, "q") or "").strip()
        spec.filters = [_parse_filter(raw, specs) for raw in _getlist(params, "f") if raw]
        sort_values = _getlist(params, "sort") or list(default_sort)
        spec.sort = []
        for raw in sort_values:
            for part in str(raw).split(","):
                part = part.strip()
                if part:
                    spec.sort.append(_parse_sort(part, specs))
        return spec

    def to_params(self) -> dict[str, Any]:
        """Round-trip form (used by links and, later, SavedView)."""
        params: dict[str, Any] = {}
        if self.q:
            params["q"] = self.q
        if self.filters:
            params["f"] = [f"{term.field}:{term.op}:{term.value}" for term in self.filters]
        if self.sort:
            params["sort"] = ",".join(
                f"-{term.field}" if term.descending else term.field for term in self.sort
            )
        if self.page > 1:
            params["page"] = self.page
        if self.size:
            params["size"] = self.size
        return params

    def to_query_string(self) -> str:
        pairs: list[tuple[str, str]] = []
        for key, value in self.to_params().items():
            if isinstance(value, list):
                pairs.extend((key, str(item)) for item in value)
            else:
                pairs.append((key, str(value)))
        return urlencode(pairs)

    # -- execution ----------------------------------------------------------
    def apply(
        self,
        stmt: Select[Any],
        specs: Mapping[str, FieldSpec],
        *,
        search_specs: Sequence[FieldSpec] = (),
    ) -> Select[Any]:
        for term in self.filters:
            spec = specs[term.field]
            column = spec.column
            if column is None:
                raise QuerySpecError(f"Field {term.field!r} cannot be filtered.")
            try:
                stmt = stmt.where(spec.type.build_filter(column, term.op, term.value))
            except FieldFilterError as exc:
                raise QuerySpecError(str(exc)) from exc
        if self.q and search_specs:
            conditions = [
                spec.type.build_search(spec.column, self.q)
                for spec in search_specs
                if spec.column is not None
            ]
            if conditions:
                stmt = stmt.where(or_(*conditions))
        for term in self.sort:
            column = specs[term.field].column
            if column is None:
                raise QuerySpecError(f"Field {term.field!r} cannot be sorted.")
            stmt = stmt.order_by(column.desc() if term.descending else column.asc())
        return stmt


def _parse_filter(raw: str, specs: Mapping[str, FieldSpec]) -> FilterTerm:
    field_name, _, remainder = raw.partition(":")
    if not field_name or not remainder:
        raise QuerySpecError(f"Invalid filter {raw!r}: expected field:op:value.")
    if ":" in remainder:
        op, _, value = remainder.partition(":")
    else:
        op, value = "eq", remainder
    spec = specs.get(field_name)
    if spec is None:
        raise QuerySpecError(f"Unknown filter field {field_name!r}.")
    if spec.column is None:
        raise QuerySpecError(f"Field {field_name!r} cannot be filtered.")
    if op not in spec.type.operators:
        raise QuerySpecError(f"Operator {op!r} is not allowed for field {field_name!r}.")
    return FilterTerm(field=field_name, op=op, value=_parse_value(spec, op, value))


def _parse_value(spec: FieldSpec, op: str, value: str) -> Any:
    if op == "isnull":
        if value == "":
            return True
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
        raise QuerySpecError(f"Filter {spec.name!r}: 'isnull' expects true or false.")
    if op == "in":
        return [spec.type.from_input(part.strip(), spec) for part in value.split(",")]
    with_context: Any = spec.type.from_input(value, spec)
    if op in {"eq", "ne"} and with_context in (None, ""):
        raise QuerySpecError(f"Filter {spec.name!r}: a value is required for {op!r}.")
    return with_context


def _parse_sort(raw: str, specs: Mapping[str, FieldSpec]) -> SortTerm:
    descending = raw.startswith("-")
    field_name = raw[1:] if descending else raw
    spec = specs.get(field_name)
    if spec is None:
        raise QuerySpecError(f"Unknown sort field {field_name!r}.")
    if not spec.type.sortable or spec.column is None:
        raise QuerySpecError(f"Field {field_name!r} cannot be sorted.")
    return SortTerm(field=field_name, descending=descending)


def _get(params: Any, key: str) -> str | None:
    getter = getattr(params, "get", None)
    if getter is None:
        raise TypeError("params must support .get()")
    value = getter(key)
    return None if value is None else str(value)


def _getlist(params: Any, key: str) -> list[str]:
    getlist = getattr(params, "getlist", None)
    if getlist is not None:
        return [str(item) for item in getlist(key)]
    value = params.get(key) if hasattr(params, "get") else None
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def _clamp_page(raw: str | None) -> int:
    try:
        page = int(raw) if raw not in (None, "") else 1
    except (TypeError, ValueError):
        raise QuerySpecError("'page' must be an integer.") from None
    return max(page, 1)


def _clamp_size(raw: str | None, default: int, maximum: int) -> int:
    try:
        size = int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        raise QuerySpecError("'size' must be an integer.") from None
    return min(max(size, 1), maximum)
