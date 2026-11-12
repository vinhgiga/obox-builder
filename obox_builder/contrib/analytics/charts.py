"""Immutable chart declarations shared by class configuration and shortcuts."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

import sqlalchemy as sa

from obox_builder.builder.aggregates import Aggregation, Measure
from obox_builder.builder.filters import FilterGroup, FilterTerm
from obox_builder.errors import ImproperlyConfigured

if TYPE_CHECKING:
    from obox_builder.context import Context

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


def Count() -> Measure:  # noqa: N802
    return Measure()


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
class Chart:
    # The first eight arguments preserve existing dashboard declarations.
    slug: str
    kind: Kind
    x: str | Dim
    measure: Measure = field(default_factory=Measure)
    title: str = ""
    filters: FilterGroup = field(default_factory=FilterGroup)
    sort: Literal["group", "value"] = "group"
    descending: bool = False
    y: tuple[Series, ...] | None = None
    by: str | Dim | None = None
    source: type[Any] | None = None
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
    _definition: type[ChartBuilder] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.statement is not None and not callable(self.statement):
            raise ImproperlyConfigured("Chart statement must be a callable accepting Context.")
        if self.y is None and (
            self.kind == "scatter"
            or self.by is not None
            or self.description
            or self.order != "x"
            or self.orientation != "vertical"
            or self.height != "md"
            or self.stack != "none"
            or self.legend != "auto"
            or self.labels
            or self.percent
            or not self.drill
            or self.primary_axis
            or self.secondary_axis
            or self.statement is not None
        ):
            object.__setattr__(self, "y", (Series(self.measure),))

    @property
    def dimension(self) -> Dim:
        return self.x if isinstance(self.x, Dim) else Dim(self.x)

    @property
    def group(self) -> Dim | None:
        return self.by if isinstance(self.by, Dim) else Dim(self.by) if self.by else None

    @property
    def series(self) -> tuple[Series, ...]:
        return self.y if self.y is not None else (Series(self.measure),)

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

    @property
    def aggregation(self) -> Aggregation:
        return Aggregation(
            self.dimension.field,
            self.measure,
            self.group.field if self.group else None,
            row_sort="group" if self.kind == "line" else self.sort,
            row_descending=False if self.kind == "line" else self.descending,
        )

    def validate(self, builder: Any) -> None:
        if self.y is None:
            self.aggregation.validate(builder)
            return
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


class ChartBuilder:
    slug: str | None = None
    title = ""
    description = ""
    type: Kind = "bar"

    def get_statement(self, ctx: Context) -> sa.Select[Any] | None:
        """Optionally narrow the source selection; resource policies still apply."""
        return None

    class Data:
        source: type[Any] | None = None
        x: str | Dim = ""
        y: Any = Measure()
        color: str | Dim | None = None
        where: FilterGroup | Mapping[str, Any] | None = None
        order = "x"

    class Appearance:
        orientation = "vertical"
        height = "md"
        stack = "none"
        legend = "auto"
        labels = False
        percent = False
        primary_axis: Axis | None = None
        secondary_axis: Axis | None = None

    class Action:
        drill = False

    @classmethod
    def as_chart(cls) -> Chart:
        sections: dict[str, dict[str, Any]] = {}
        for section in ("Data", "Appearance", "Action"):
            values: dict[str, Any] = {}
            allowed = {
                key for key in vars(getattr(ChartBuilder, section)) if not key.startswith("_")
            }
            for base in reversed(cls.__mro__):
                configuration = vars(base).get(section)
                if configuration is None:
                    continue
                if not isinstance(configuration, type):
                    raise ImproperlyConfigured(f"{section} must be a configuration class.")
                for config_base in reversed(configuration.__mro__):
                    if base is not ChartBuilder and config_base is getattr(ChartBuilder, section):
                        continue
                    for key, value in vars(config_base).items():
                        if not key.startswith("_"):
                            if key not in allowed:
                                raise ImproperlyConfigured(f"Unknown {section} option: {key}.")
                            values[key] = value
            sections[section] = values
        data = sections["Data"]
        slug = (
            cls.slug
            if cls.slug is not None
            else re.sub(
                r"(?<!^)(?=[A-Z])",
                "_",
                cls.__name__.removesuffix("Chart"),
            ).lower()
        )
        return chart(
            cls.type,
            data["source"],
            x=data["x"],
            y=data["y"],
            color=data["color"],
            slug=slug,
            title=cls.title or slug.replace("_", " ").title(),
            description=cls.description,
            where=data["where"],
            order=data["order"],
            _definition=cls,
            **sections["Appearance"],
            **sections["Action"],
        )


def chart(
    kind: Kind,
    source: type[Any] | None = None,
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
) -> Chart:
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
    return Chart(
        slug,
        kind,
        x,
        y=series,
        by=color,
        source=source,
        filters=_where(where) or FilterGroup(),
        **options,
    )


def bar(source: type[Any] | None = None, **options: Any) -> Chart:
    return chart("bar", source, **options)


def line(source: type[Any] | None = None, **options: Any) -> Chart:
    return chart("line", source, **options)


def scatter(source: type[Any] | None = None, **options: Any) -> Chart:
    return chart("scatter", source, **options)


def pie(source: type[Any] | None = None, **options: Any) -> Chart:
    return chart("pie", source, **options)


def donut(source: type[Any] | None = None, **options: Any) -> Chart:
    return chart("donut", source, **options)


def _terms(group: FilterGroup) -> Iterator[FilterTerm]:
    for child in group.children:
        if isinstance(child, FilterTerm):
            yield child
        else:
            yield from _terms(child)
