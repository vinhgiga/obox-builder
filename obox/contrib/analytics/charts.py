"""Immutable chart declarations shared by class configuration and shortcuts."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

import sqlalchemy as sa

from obox.contrib.analytics.aggregates import FieldCount, Measure
from obox.core.exceptions import ImproperlyConfigured
from obox.db.filters import FilterGroup, FilterTerm

if TYPE_CHECKING:
    from obox.runtime.context import Context

Kind = Literal["bar", "line", "scatter", "pie", "donut"]
KINDS = {"bar", "line", "scatter", "pie", "donut"}
_COUNT = Measure()


def _where(value: FilterGroup | Mapping[str, Any] | None) -> FilterGroup | None:
    if value is None or isinstance(value, FilterGroup):
        return value
    if not isinstance(value, Mapping):
        raise ImproperlyConfigured("Use a FilterGroup or a mapping of field lookups.")
    terms = []
    for lookup, operand in value.items():
        if not isinstance(lookup, str) or not lookup:
            raise ImproperlyConfigured("Filter lookups must be nonempty strings.")
        name, separator, operator = lookup.rpartition("__")
        terms.append(
            FilterTerm(name if separator else lookup, operator if separator else "is", operand)
        )
    return FilterGroup(children=tuple(terms))


def Count(name: str | None = None, *, label: str | None = None) -> Measure:  # noqa: N802
    return Measure(label=label) if name is None else FieldCount("count", name, label)


def Sum(name: str) -> Measure:  # noqa: N802
    return Measure("sum", name)


def Avg(name: str) -> Measure:  # noqa: N802
    return Measure("avg", name)


def Min(name: str) -> Measure:  # noqa: N802
    return Measure("min", name)


def Max(name: str) -> Measure:  # noqa: N802
    return Measure("max", name)


@dataclass(frozen=True)
class Field:
    name: str


@dataclass(frozen=True)
class Dim:
    field: str
    label: str | None = None
    include_empty: bool = True


@dataclass(frozen=True, init=False)
class Series:
    value: Measure | Field
    id: str
    label: str | None = None
    axis: Literal["primary", "secondary"] = "primary"
    kind: Kind | None = None
    where: FilterGroup | None = None

    def __init__(
        self,
        value: Measure | Field | str = _COUNT,
        *,
        id: str | None = None,
        label: str | None = None,
        axis: Literal["primary", "secondary"] = "primary",
        kind: Kind | None = None,
        where: FilterGroup | Mapping[str, Any] | None = None,
    ) -> None:
        value = Field(value) if isinstance(value, str) else value
        if not isinstance(value, (Measure, Field)):
            raise ImproperlyConfigured("Use a numeric Field or an explicit Measure.")
        identifier = (
            value.name
            if isinstance(value, Field)
            else "_".join(part for part in (value.operation, value.field) if part)
        )
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "id", identifier if id is None else id)
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "axis", axis)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "where", _where(where))
        if (
            not isinstance(self.id, str)
            or re.fullmatch(r"[a-z][a-z0-9_]*", self.id) is None
            or self.axis not in {"primary", "secondary"}
            or (self.kind is not None and self.kind not in KINDS)
            or (self.label is not None and not isinstance(self.label, str))
            or (self.where is not None and not isinstance(self.where, FilterGroup))
        ):
            raise ImproperlyConfigured("Invalid chart series.")

    @property
    def measure(self) -> Measure:
        if not isinstance(self.value, Measure):
            raise ImproperlyConfigured("Raw series do not have an aggregate measure.")
        return self.value


@dataclass(frozen=True)
class Axis:
    label: str | None = None
    zero: bool | None = None
    minimum: float | None = None
    maximum: float | None = None

    def __post_init__(self) -> None:
        if (
            (self.label is not None and not isinstance(self.label, str))
            or (self.zero is not None and not isinstance(self.zero, bool))
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in (self.minimum, self.maximum)
                if value is not None
            )
            or (
                self.minimum is not None
                and self.maximum is not None
                and self.minimum >= self.maximum
            )
        ):
            raise ImproperlyConfigured("Use finite, increasing axis bounds.")


@dataclass(frozen=True)
class ChartSpec:
    slug: str
    kind: Kind
    x: str | Dim
    title: str = ""
    filters: FilterGroup = field(default_factory=FilterGroup)
    y: tuple[Series, ...] = ()
    by: str | Dim | None = None
    description: str = ""
    order: str = "x"
    orientation: str = "vertical"
    height: str = "md"
    stack: str = "none"
    legend: str = "auto"
    labels: bool = False
    percent: bool = False
    drill: bool = True
    primary_axis: Axis | None = None
    secondary_axis: Axis | None = None
    statement: Callable[[Context], Any] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.statement is not None and not callable(self.statement):
            raise ImproperlyConfigured("Chart statement must be a callable accepting Context.")

    @property
    def dimension(self) -> Dim:
        return self.x if isinstance(self.x, Dim) else Dim(self.x)

    @property
    def group(self) -> Dim | None:
        return self.by if isinstance(self.by, Dim) else Dim(self.by) if self.by else None

    @property
    def series(self) -> tuple[Series, ...]:
        return self.y

    @property
    def raw(self) -> bool:
        return isinstance(self.series[0].value, Field)

    @property
    def fields(self) -> tuple[str, ...]:
        names = [self.dimension.field]
        if self.group:
            names.append(self.group.field)
        for series in self.series:
            name = series.value.name if isinstance(series.value, Field) else series.value.field
            if name:
                names.append(name)
            if series.where is not None:
                names.extend(term.field for term in _terms(series.where))
        return tuple(dict.fromkeys(names))

    def validate(self, builder: Any) -> None:
        if (
            self.kind not in KINDS
            or not self.series
            or any(not isinstance(series, Series) for series in self.series)
            or not any(series.axis == "primary" for series in self.series)
            or len({series.id for series in self.series}) != len(self.series)
            or any(isinstance(series.value, Field) != self.raw for series in self.series)
            or (self.raw and any(series.where is not None for series in self.series))
            or self.orientation not in {"vertical", "horizontal"}
            or (self.orientation == "horizontal" and self.kind != "bar")
            or self.height not in {"sm", "md", "lg"}
            or self.stack not in {"none", "normal", "percent"}
            or self.legend not in {"auto", "hidden", "left", "right", "top", "bottom"}
            or any(not isinstance(flag, bool) for flag in (self.labels, self.percent, self.drill))
            or not isinstance(self.description, str)
            or not isinstance(self.title, str)
            or (self.by is not None and not isinstance(self.by, (str, Dim)))
            or self.by == ""
            or any(
                axis is not None and not isinstance(axis, Axis)
                for axis in (self.primary_axis, self.secondary_axis)
            )
        ):
            raise ImproperlyConfigured("Invalid chart configuration.")
        for dimension in (self.dimension, self.group):
            if dimension is not None and (
                not isinstance(dimension.field, str)
                or not dimension.field
                or not isinstance(dimension.include_empty, bool)
                or (dimension.label is not None and not isinstance(dimension.label, str))
            ):
                raise ImproperlyConfigured("Invalid chart dimension.")
        for field_name in self.fields:
            spec = builder.specs.get(field_name)
            if (
                spec is None
                or field_name not in builder.fields
                or isinstance(spec.column.type, sa.JSON)
            ):
                raise ImproperlyConfigured("Charts require published scalar model columns.")
        for series in self.series:
            value = series.value
            name = value.name if isinstance(value, Field) else value.field
            if isinstance(value, Measure) and (
                value.operation == "count_numbers"
                or (value.operation == "count" and name is not None)
            ):
                raise ImproperlyConfigured("Chart count measures count records.")
            if name:
                kind = builder.specs[name].column.type
                if isinstance(kind, sa.Boolean) or not isinstance(kind, (sa.Integer, sa.Numeric)):
                    raise ImproperlyConfigured("Chart values require numeric fields.")
            if series.kind not in {None, self.kind} and (
                series.axis == "primary" or series.kind in {"pie", "donut"}
            ):
                raise ImproperlyConfigured(
                    "Only secondary series may use a different Cartesian kind."
                )
        circular = self.kind in {"pie", "donut"}
        if circular and (
            self.raw
            or len(self.series) != 1
            or self.group
            or self.stack != "none"
            or self.primary_axis
            or self.secondary_axis
        ):
            raise ImproperlyConfigured(
                "Circular charts require one aggregate and no axes/grouping."
            )
        if (self.percent and not circular) or (
            self.stack != "none"
            and (
                self.kind != "bar"
                or self.raw
                or any(s.kind not in {None, "bar"} for s in self.series)
            )
        ):
            raise ImproperlyConfigured("Invalid chart percentages or stacking.")
        if self.stack == "percent" and any(
            axis is not None
            and (
                axis.minimum not in {None, 0} or axis.maximum not in {None, 1} or axis.zero is False
            )
            for axis in (self.primary_axis, self.secondary_axis)
        ):
            raise ImproperlyConfigured("Percent stack axes must use bounds from zero to one.")
        target = self.order.removeprefix("-") if isinstance(self.order, str) else ""
        if target not in {"source", "x", "y", *[f"y:{s.id}" for s in self.series]} or (
            target == "source" and self.order != "source"
        ):
            raise ImproperlyConfigured(
                "Order by source, x, y, or y:<seriesId>, optionally descending."
            )


def chart_spec(
    kind: Kind,
    *,
    x: str | Dim,
    y: Measure
    | Field
    | Series
    | str
    | Sequence[Measure | Field | Series | str]
    | Mapping[str, Measure | Field | Series | str] = _COUNT,
    color: str | Dim | None = None,
    slug: str | None = None,
    where: FilterGroup | Mapping[str, Any] | None = None,
    **options: Any,
) -> ChartSpec:
    """Declare a chart; mapping y keys label series identified by their position."""
    labels = tuple(y) if isinstance(y, Mapping) else None
    values = (
        tuple(y.values())
        if isinstance(y, Mapping)
        else ((y,) if isinstance(y, (Measure, Field, Series, str)) else tuple(y))
    )
    if not values:
        raise ImproperlyConfigured("A chart requires at least one series.")
    series = tuple(value if isinstance(value, Series) else Series(value) for value in values)
    if labels is not None:
        series = tuple(
            replace(item, id=f"series_{index}", label=label)
            for index, (label, item) in enumerate(zip(labels, series, strict=True))
        )
    options.setdefault("drill", False)
    slug = f"{kind}_{x.field if isinstance(x, Dim) else x}" if slug is None else slug
    options.setdefault(
        "title",
        slug.replace("_", " ").title(),
    )
    return ChartSpec(
        slug,
        kind,
        x,
        y=series,
        by=color,
        filters=_where(where) or FilterGroup(),
        **options,
    )


def _terms(group: FilterGroup) -> Iterator[FilterTerm]:
    for child in group.children:
        if isinstance(child, FilterTerm):
            yield child
        else:
            yield from _terms(child)
