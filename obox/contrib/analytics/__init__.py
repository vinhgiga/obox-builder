"""Typed analytical components over registered resource builders."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

from obox._components import Component, ComponentQuery
from obox._exceptions import OboxError
from obox.contrib.analytics.charts import (
    Avg,
    Axis,
    ChartSpec,
    Count,
    Dim,
    Field,
    Kind,
    Max,
    Min,
    Series,
    Sum,
    chart_spec,
)
from obox.contrib.analytics.schemas import ChartData, PivotData
from obox.contrib.resources.aggregates import Aggregation, Measure
from obox.contrib.resources.filters import FilterGroup
from obox.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from obox._context import Context
    from obox._runtime import _Runtime
    from obox.contrib.resources.components import ComponentService

__all__ = [
    "Analytics",
    "Avg",
    "Axis",
    "Bar",
    "Chart",
    "Count",
    "Dim",
    "Donut",
    "Field",
    "Line",
    "Max",
    "Measure",
    "Min",
    "Pie",
    "Pivot",
    "Scatter",
    "Series",
    "Sum",
]


@dataclass(frozen=True)
class PivotSpec:
    slug: str
    rows: str
    measure: Measure = field(default_factory=Measure)
    columns: str | None = None
    title: str = ""
    filters: FilterGroup = field(default_factory=FilterGroup)
    row_sort: Literal["group", "value"] = "group"
    column_sort: Literal["group", "value"] = "group"
    row_descending: bool = False
    column_descending: bool = False
    show_row_totals: bool = True
    show_column_totals: bool = True

    @property
    def aggregation(self) -> Aggregation:
        return Aggregation(
            self.rows,
            self.measure,
            self.columns,
            self.row_sort,
            self.column_sort,
            self.row_descending,
            self.column_descending,
        )


class Chart(Component):
    plugin = "analytics"
    kind = "bar"
    query_model = ComponentQuery
    result_model = ChartData
    x: str | Dim = ""
    y: Any = Measure()
    color: str | Dim | None = None
    order = "x"
    orientation = "vertical"
    height = "md"
    stack = "none"
    legend = "auto"
    labels = False
    percent = False
    drill = False
    primary_axis: Axis | None = None
    secondary_axis: Axis | None = None

    def get_statement(self, ctx: Context) -> Any:
        """Narrow the authorized source; never replace its policy selection."""
        return None

    def _build(self, builder: Any, name: str) -> ChartSpec:
        spec = chart_spec(
            cast(Kind, self.kind),
            x=self.x,
            y=self.y,
            color=self.color,
            slug=name,
            title=self.title,
            description=self.description,
            where=self.filters,
            order=self.order,
            orientation=self.orientation,
            height=self.height,
            stack=self.stack,
            legend=self.legend,
            labels=self.labels,
            percent=self.percent,
            drill=self.drill,
            primary_axis=self.primary_axis,
            secondary_axis=self.secondary_axis,
        )
        spec.validate(builder)
        return spec

    async def _execute(self, service: ComponentService, query: Any, ctx: Context) -> Any:
        from obox.contrib.analytics.service import execute

        return await execute(service, query, ctx, self)

    async def _records(self, service: ComponentService, query: Any, ctx: Context) -> Any:
        from obox.contrib.analytics.service import records

        return await records(service, query, ctx, self)

    def _presentation(self, spec: Any) -> dict[str, Any]:
        from dataclasses import asdict

        return {
            **{
                name: getattr(spec, name)
                for name in (
                    "kind",
                    "orientation",
                    "stack",
                    "legend",
                    "labels",
                    "percent",
                    "drill",
                )
            },
            "primaryAxis": asdict(spec.primary_axis) if spec.primary_axis else {},
            "secondaryAxis": asdict(spec.secondary_axis) if spec.secondary_axis else {},
            "height": self.height,
        }


class Bar(Chart):
    kind = "bar"


class Line(Chart):
    kind = "line"


class Scatter(Chart):
    kind = "scatter"


class Pie(Chart):
    kind = "pie"


class Donut(Chart):
    kind = "donut"


class Pivot(Component):
    plugin = "analytics"
    kind = "pivot"
    query_model = ComponentQuery
    result_model = PivotData
    rows = ""
    columns: str | None = None
    measure = Measure()
    row_sort: Literal["group", "value"] = "group"
    column_sort: Literal["group", "value"] = "group"
    row_descending = False
    column_descending = False
    show_row_totals = True
    show_column_totals = True
    drill = False
    height = "md"

    def _build(self, builder: Any, name: str) -> PivotSpec:
        spec = PivotSpec(
            name,
            self.rows,
            self.measure,
            self.columns,
            self.title,
            cast(FilterGroup, self.filters),
            self.row_sort,
            self.column_sort,
            self.row_descending,
            self.column_descending,
            self.show_row_totals,
            self.show_column_totals,
        )
        spec.aggregation.validate(builder)
        if any(
            type(flag) is not bool
            for flag in (
                self.show_row_totals,
                self.show_column_totals,
                self.drill,
            )
        ) or self.height not in {"sm", "md", "lg"}:
            raise ImproperlyConfigured("Invalid pivot presentation.")
        return spec

    async def _execute(self, service: ComponentService, query: Any, ctx: Context) -> Any:
        from obox.contrib.analytics.service import execute

        return await execute(service, query, ctx, self)

    async def _records(self, service: ComponentService, query: Any, ctx: Context) -> Any:
        from obox.contrib.analytics.service import records

        return await records(service, query, ctx, self)

    def _presentation(self, spec: Any) -> dict[str, Any]:
        return {
            "kind": "pivot",
            "drill": self.drill,
            "height": self.height,
            "rowTotals": self.show_row_totals,
            "columnTotals": self.show_column_totals,
        }


class AggregateTimeout(OboxError):
    status = 503
    code = "aggregate_timeout"


class Analytics:
    name = "analytics"
    requires_resources = True
    requires: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        max_rows: int = 200,
        max_columns: int = 50,
        max_cells: int = 10_000,
        max_chart_groups: int = 200,
        max_pie_groups: int = 50,
        max_points: int = 10_000,
        timeout_ms: int = 3000,
    ) -> None:
        limits = (
            max_rows,
            max_columns,
            max_cells,
            max_chart_groups,
            max_pie_groups,
            max_points,
            timeout_ms,
        )
        if any(type(value) is not int or value < 1 for value in limits):
            raise ImproperlyConfigured("Analytics limits must be positive integers.")
        self.max_rows = max_rows
        self.max_columns = max_columns
        self.max_cells = max_cells
        self.max_chart_groups = max_chart_groups
        self.max_pie_groups = max_pie_groups
        self.max_points = max_points
        self.timeout_ms = timeout_ms

    def _attach(self, obox: _Runtime) -> None:
        pass
