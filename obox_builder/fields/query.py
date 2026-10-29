"""QuerySpec: parse and whitelist list-page query parameters into SQL.

Parsing and validation are pure Python (no database needed); ``apply`` turns the
spec into SQLAlchemy WHERE/ORDER BY clauses. Invalid input raises
:class:`QuerySpecError` so views can answer 400 instead of guessing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlencode

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Enum, Select, String, and_, cast, or_
from sqlalchemy.sql.elements import ColumnElement

from obox_builder.db.collation import VietnameseOrder
from obox_builder.fields.base import FieldFilterError, FieldSpec

__all__ = ["FilterGroup", "FilterTerm", "QuerySpec", "QuerySpecError", "SortTerm"]

MAX_FILTERS = 100
MAX_DEPTH = 3
MAX_SORTS = 5
MAX_GROUPS = 3
MAX_SEARCH = 200


class QuerySpecError(ValueError):
    """The query string asked for something the field specs do not allow."""


@dataclass(frozen=True)
class FilterTerm:
    field: str
    op: str
    value: Any


@dataclass(frozen=True)
class FilterGroup:
    conjunction: str = "and"
    children: tuple[FilterTerm | FilterGroup, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "conjunction": self.conjunction,
            "children": [
                child.to_dict()
                if isinstance(child, FilterGroup)
                else {
                    "field": child.field,
                    "op": child.op,
                    "value": _input_value(child.value),
                }
                for child in self.children
            ],
        }


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
    filter_group: FilterGroup | None = None
    group: list[SortTerm] = field(default_factory=list)

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
        if len(spec.q) > MAX_SEARCH:
            raise QuerySpecError(f"Search is limited to {MAX_SEARCH} characters.")
        raw_filters = [raw for raw in _getlist(params, "f") if raw]
        if len(raw_filters) > MAX_FILTERS:
            raise QuerySpecError(f"At most {MAX_FILTERS} filters are allowed.")
        spec.filters = [_parse_filter(raw, specs) for raw in raw_filters]
        raw_tree = _get(params, "filter")
        if raw_tree:
            if len(raw_tree) > 32_000:
                raise QuerySpecError("Filter configuration is too large.")
            try:
                tree = json.loads(raw_tree)
            except (ValueError, RecursionError) as exc:
                raise QuerySpecError("Invalid filter configuration.") from exc
            depth = (
                2
                if spec.filters and isinstance(tree, dict) and tree.get("conjunction") == "or"
                else 1
            )
            spec.filter_group = _parse_group(tree, specs, depth, [len(spec.filters)])
        sort_values = _getlist(params, "sort") or list(default_sort)
        spec.sort = []
        for raw in sort_values:
            for part in str(raw).split(","):
                part = part.strip()
                if part:
                    spec.sort.append(_parse_sort(part, specs))
        if len(spec.sort) > MAX_SORTS:
            raise QuerySpecError(f"At most {MAX_SORTS} sort fields are allowed.")
        _unique_terms(spec.sort, "sort")
        for raw in _getlist(params, "group"):
            spec.group.extend(
                _parse_sort(part.strip(), specs) for part in raw.split(",") if part.strip()
            )
        if len(spec.group) > MAX_GROUPS:
            raise QuerySpecError(f"At most {MAX_GROUPS} group fields are allowed.")
        _unique_terms(spec.group, "group")
        return spec

    def combined_filters(self) -> FilterGroup:
        tree = self.filter_group or FilterGroup()
        if not self.filters:
            return tree
        children = tree.children if tree.conjunction == "and" else (tree,)
        return FilterGroup(children=(*self.filters, *children))

    def to_params(self) -> dict[str, Any]:
        """Round-trip form used by links and private saved views."""
        params: dict[str, Any] = {}
        if self.q:
            params["q"] = self.q
        if self.filters:
            params["f"] = [
                f"{term.field}:{term.op}:{_legacy_value(term.value)}" for term in self.filters
            ]
        if self.filter_group is not None:
            params["filter"] = json.dumps(self.filter_group.to_dict(), ensure_ascii=False)
        if self.group:
            params["group"] = ",".join(
                f"{'-' if term.descending else ''}{term.field}" for term in self.group
            )
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
        text_collation: Literal["vi"] | None = None,
    ) -> Select[Any]:
        for filter_term in self.filters:
            stmt = stmt.where(_condition(filter_term, specs))
        if self.filter_group is not None:
            stmt = stmt.where(_condition(self.filter_group, specs))
        if self.q and search_specs:
            conditions = []
            for spec in search_specs:
                if spec.column is None or not spec.type.searchable or spec.secret:
                    continue
                mode = spec.options.get("search_mode", "contains")
                condition: ColumnElement[bool]
                if mode in {"exact", "startswith"}:
                    from obox_builder.db.search import SearchText, fold_text
                    from obox_builder.fields.base import like_escape

                    term = like_escape(fold_text(self.q) or "")
                    if mode == "startswith":
                        term += "%"
                    condition = SearchText(spec.column).like(term, escape="\\")
                else:
                    condition = spec.type.build_search(spec.column, self.q)
                conditions.append(_related_condition(spec, condition))
            if conditions:
                stmt = stmt.where(or_(*conditions))
        ordered_terms = self.group + [
            sort_term
            for sort_term in self.sort
            if sort_term.field not in {group_term.field for group_term in self.group}
        ]
        if ordered_terms:
            stmt = stmt.order_by(None)
        for sort_term in ordered_terms:
            column = specs[sort_term.field].column
            if column is None:
                raise QuerySpecError(f"Field {sort_term.field!r} cannot be sorted.")
            if text_collation == "vi" and isinstance(column.type, String):
                text_column = cast(column, String) if isinstance(column.type, Enum) else column
                column = VietnameseOrder(text_column)
            stmt = stmt.order_by(
                (column.desc() if sort_term.descending else column.asc()).nulls_last()
            )
        names = {sort_term.field for sort_term in ordered_terms}
        for spec in specs.values():
            if spec.primary_key and spec.column is not None and spec.name not in names:
                stmt = stmt.order_by(spec.column.desc())
        return stmt


def _condition(
    node: FilterTerm | FilterGroup, specs: Mapping[str, FieldSpec]
) -> ColumnElement[bool]:
    if isinstance(node, FilterGroup):
        if not node.children:
            from sqlalchemy import true

            return true()
        conditions = [_condition(child, specs) for child in node.children]
        return (and_ if node.conjunction == "and" else or_)(*conditions)
    spec = specs.get(node.field)
    if spec is None or spec.column is None or spec.secret or spec.options.get("computed"):
        raise QuerySpecError(f"Field {node.field!r} cannot be filtered.")
    try:
        condition = spec.type.build_filter(spec.column, node.op, node.value)
        return _related_condition(spec, condition)
    except FieldFilterError as exc:
        raise QuerySpecError(str(exc)) from exc


def _related_condition(spec: FieldSpec, condition: ColumnElement[bool]) -> ColumnElement[bool]:
    path = spec.options.get("relationship_path", ())
    scopes = spec.options.get("relationship_scopes", ())
    for relationship, scope in reversed(list(zip(path, scopes, strict=True))):
        target_pk = relationship.property.mapper.primary_key[0]
        condition = and_(target_pk.in_(scope), condition)
        condition = (
            relationship.any(condition)
            if relationship.property.uselist
            else relationship.has(condition)
        )
    return condition


def _parse_group(
    raw: Any, specs: Mapping[str, FieldSpec], depth: int, count: list[int]
) -> FilterGroup:
    if depth > MAX_DEPTH or not isinstance(raw, dict):
        raise QuerySpecError(f"Filters support at most {MAX_DEPTH} group levels.")
    if set(raw) != {"conjunction", "children"} or raw["conjunction"] not in ("and", "or"):
        raise QuerySpecError("A filter group requires conjunction 'and' or 'or' and children.")
    if not isinstance(raw["children"], list) or len(raw["children"]) > MAX_FILTERS:
        raise QuerySpecError(f"At most {MAX_FILTERS} filters are allowed.")
    children: list[FilterTerm | FilterGroup] = []
    for child in raw["children"]:
        count[0] += 1
        if count[0] > MAX_FILTERS or not isinstance(child, dict):
            raise QuerySpecError(f"At most {MAX_FILTERS} filter nodes are allowed.")
        if "children" in child:
            subgroup = _parse_group(child, specs, depth + 1, count)
            if subgroup.children:
                children.append(subgroup)
        elif set(child) == {"field", "op", "value"}:
            name, op = child["field"], child["op"]
            if not isinstance(name, str) or not isinstance(op, str):
                raise QuerySpecError("Filter field and operator must be strings.")
            spec = _filter_spec(name, op, specs)
            children.append(FilterTerm(name, op, _parse_value(spec, op, child["value"])))
        else:
            raise QuerySpecError("A filter requires field, op and value.")
    return FilterGroup(raw["conjunction"], tuple(children))


def _unique_terms(terms: list[SortTerm], kind: str) -> None:
    if len({term.field for term in terms}) != len(terms):
        raise QuerySpecError(f"Duplicate {kind} field.")


def _input_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_input_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _legacy_value(value: Any) -> str:
    if isinstance(value, list):
        return ",".join(_legacy_value(item) for item in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(_input_value(value))


def _parse_filter(raw: str, specs: Mapping[str, FieldSpec]) -> FilterTerm:
    field_name, _, remainder = raw.partition(":")
    if not field_name or not remainder:
        raise QuerySpecError(f"Invalid filter {raw!r}: expected field:op:value.")
    if ":" in remainder:
        op, _, value = remainder.partition(":")
    else:
        op, value = "eq", remainder
    spec = _filter_spec(field_name, op, specs)
    return FilterTerm(field=field_name, op=op, value=_parse_value(spec, op, value))


def _filter_spec(name: str, op: str, specs: Mapping[str, FieldSpec]) -> FieldSpec:
    spec = specs.get(name)
    if spec is None or spec.secret or spec.options.get("computed"):
        raise QuerySpecError(f"Unknown filter field {name!r}.")
    if spec.column is None:
        raise QuerySpecError(f"Field {name!r} cannot be filtered.")
    if op not in spec.type.operators:
        raise QuerySpecError(f"Operator {op!r} is not allowed for field {name!r}.")
    return spec


def _parse_value(spec: FieldSpec, op: str, value: Any) -> Any:
    if op == "isnull":
        normalized = str(value).strip().lower()
        if normalized in {"", "true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
        raise QuerySpecError(f"Filter {spec.name!r}: 'isnull' expects true or false.")
    if op == "in":
        values = value.split(",") if isinstance(value, str) else value
        if not isinstance(values, list) or not values or len(values) > MAX_FILTERS:
            raise QuerySpecError(f"Filter {spec.name!r}: 'in' expects 1–{MAX_FILTERS} values.")
        return [_scalar_value(spec, part) for part in values]
    return _scalar_value(spec, value, partial=op in {"contains", "startswith", "endswith"})


def _scalar_value(spec: FieldSpec, value: Any, *, partial: bool = False) -> Any:
    if value is None or isinstance(value, (dict, list)):
        raise QuerySpecError(f"Filter {spec.name!r}: a value is required.")
    if spec.type.name == "checkbox" and str(value).strip().lower() not in {
        "true",
        "false",
        "1",
        "0",
        "yes",
        "no",
        "on",
        "off",
        "y",
        "n",
    }:
        raise QuerySpecError(f"Filter {spec.name!r}: expected a boolean.")
    try:
        parsed = (
            spec.type.from_partial_input(value, spec)
            if partial
            else spec.type.from_input(value, spec)
        )
        parsed = TypeAdapter(spec.type.annotation(spec)).validate_python(parsed)
        if parsed is None or parsed == "":
            raise QuerySpecError(f"Filter {spec.name!r}: a value is required.")
        if hasattr(parsed, "is_finite") and not parsed.is_finite():
            raise QuerySpecError(f"Filter {spec.name!r}: a finite value is required.")
        error = None if partial else spec.type.validate(parsed, spec)
        if error:
            raise QuerySpecError(f"Filter {spec.name!r}: {error}")
        return parsed
    except QuerySpecError:
        raise
    except (ValidationError, ValueError, TypeError) as exc:
        raise QuerySpecError(f"Filter {spec.name!r}: invalid value.") from exc


def _parse_sort(raw: str, specs: Mapping[str, FieldSpec]) -> SortTerm:
    descending = raw.startswith("-")
    field_name = raw[1:] if descending else raw
    spec = specs.get(field_name)
    if (
        spec is None
        or spec.secret
        or spec.options.get("relationship_path")
        or (spec.column is None and not spec.options.get("computed"))
    ):
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
