"""Chart and pivot component declarations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

from obox.components.declarations import Component, ComponentQuery
from obox.contrib.analytics.aggregates import FieldCount, Measure
from obox.contrib.analytics.charts import (
    Axis,
    ChartSpec,
    Dim,
    Kind,
    chart_spec,
)
from obox.contrib.analytics.pivot import PivotSpec, ValuesAxis, axis
from obox.contrib.analytics.schemas import ChartData, PivotMatrix
from obox.core.exceptions import ImproperlyConfigured
from obox.web.schemas import ApiResponse

if TYPE_CHECKING:
    from obox.components.service import ComponentService
    from obox.runtime.context import Context


def CountNumbers(name: str, *, label: str | None = None) -> Measure:  # noqa: N802
    return FieldCount("count_numbers", name, label)


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

    def build(self, builder: Any, name: str) -> ChartSpec:
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

    async def execute(self, service: ComponentService, query: Any, ctx: Context) -> Any:
        from obox.contrib.analytics.services import execute

        return await execute(service, query, ctx, self)

    async def records(self, service: ComponentService, query: Any, ctx: Context) -> Any:
        from obox.contrib.analytics.services import records

        return await records(service, query, ctx, self)

    def presentation(self, spec: Any) -> dict[str, Any]:
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
    result_model: type[ApiResponse] | None = PivotMatrix
    rows: str | tuple[str, ...] | list[str] = ""
    columns: str | tuple[str, ...] | list[str] | None = None
    values: tuple[Measure, ...] = (Measure(),)
    values_axis: ValuesAxis = "columns"
    values_position: int | None = None
    row_sort: Literal["group", "value"] = "group"
    column_sort: Literal["group", "value"] = "group"
    row_descending = False
    column_descending = False
    show_row_subtotals = True
    show_column_subtotals = True
    show_row_totals = True
    show_column_totals = True
    drill = False
    height = "md"

    def _layout(self) -> PivotSpec:
        rows, columns = axis(self.rows), axis(self.columns)
        if len(set(rows + columns)) != len(rows + columns):
            raise ImproperlyConfigured("Each pivot dimension can occur once across the axes.")
        values = self.values
        if (
            not isinstance(values, (list, tuple))
            or not values
            or any(not isinstance(value, Measure) for value in values)
        ):
            raise ImproperlyConfigured("Pivot values require at least one Measure.")
        depth = len(rows if self.values_axis == "rows" else columns)
        position = depth if self.values_position is None else self.values_position
        if (
            any(
                type(flag) is not bool
                for flag in (
                    self.show_row_totals,
                    self.show_column_totals,
                    self.show_row_subtotals,
                    self.show_column_subtotals,
                    self.row_descending,
                    self.column_descending,
                    self.drill,
                )
            )
            or self.height not in {"sm", "md", "lg"}
            or self.row_sort not in {"group", "value"}
            or self.column_sort not in {"group", "value"}
            or self.values_axis not in {"rows", "columns"}
            or type(position) is not int
            or not 0 <= position <= depth
        ):
            raise ImproperlyConfigured("Invalid pivot presentation.")
        return PivotSpec(
            rows,
            columns,
            tuple(values),
            self.values_axis,
            position,
            self.row_sort,
            self.column_sort,
            self.row_descending,
            self.column_descending,
            self.show_row_subtotals,
            self.show_column_subtotals,
            self.show_row_totals,
            self.show_column_totals,
        )

    def build(self, builder: Any, name: str) -> PivotSpec:
        spec = self._layout()
        spec.validate_model(builder)
        return spec

    async def execute(self, service: ComponentService, query: Any, ctx: Context) -> Any:
        from obox.contrib.analytics.services import execute

        return await execute(service, query, ctx, self)

    async def records(self, service: ComponentService, query: Any, ctx: Context) -> Any:
        from obox.contrib.analytics.services import records

        return await records(service, query, ctx, self)

    def presentation(self, spec: Any) -> dict[str, Any]:
        return {
            "kind": "pivot",
            "drill": self.drill,
            "height": self.height,
            "rowTotals": self.show_row_totals,
            "columnTotals": self.show_column_totals,
        }
