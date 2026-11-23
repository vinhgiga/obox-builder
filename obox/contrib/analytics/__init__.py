"""Typed analytical components over registered resource builders."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

from obox._components import Component, ComponentQuery, fixed_filters
from obox._exceptions import BuilderValidationError, OboxError
from obox._schemas import ApiResponse
from obox.contrib.analytics.aggregates import Aggregation, FieldCount, Measure
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
from obox.contrib.analytics.pivot import PivotSpec, ValuesAxis, axis
from obox.contrib.analytics.pivot_memory import MemoryRows, condition, execute_memory
from obox.contrib.analytics.schemas import ChartData, PivotData, PivotMatrix
from obox.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from obox._component_service import ComponentService
    from obox._context import Context
    from obox._runtime import _Runtime

__all__ = [
    "Analytics",
    "Avg",
    "Axis",
    "Bar",
    "Chart",
    "Count",
    "CountNumbers",
    "Dim",
    "Donut",
    "Field",
    "Line",
    "Max",
    "Measure",
    "Min",
    "Pie",
    "Pivot",
    "PivotMatrix",
    "Scatter",
    "Series",
    "Sum",
]


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
    _supports_memory = True
    plugin = "analytics"
    kind = "pivot"
    query_model = ComponentQuery
    result_model: type[ApiResponse] | None = PivotData
    data: Any = None
    rows: str | tuple[str, ...] | list[str] = ""
    columns: str | tuple[str, ...] | list[str] | None = None
    measure = Measure()
    values: tuple[Measure, ...] | list[Measure] | None = None
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

    def __init__(self, *, data: Any = None, **options: Any) -> None:
        super().__init__(**options)
        data = self.data if data is None else data
        if data is not None:
            if self.source is not None:
                raise ImproperlyConfigured("Choose a model source or in-memory pivot data.")
            self.data = MemoryRows.from_data(data, tuple(self.fields))
            self.fields = self.data.columns

    def _layout(self, *, model: bool = False) -> PivotSpec:
        rows, columns = axis(self.rows), axis(self.columns)
        if len(set(rows + columns)) != len(rows + columns):
            raise ImproperlyConfigured("Each pivot dimension can occur once across the axes.")
        values = self.values if self.values is not None else (self.measure,)
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
        legacy = None
        if (
            model
            and isinstance(self.rows, str)
            and self.rows
            and (self.columns is None or isinstance(self.columns, str))
            and self.values is None
            and self.values_axis == "columns"
            and self.values_position is None
            and self.measure.label is None
            and self.measure.operation != "count_numbers"
            and not (self.measure.operation == "count" and self.measure.field is not None)
        ):
            legacy = Aggregation(
                self.rows,
                self.measure,
                self.columns,
                self.row_sort,
                self.column_sort,
                self.row_descending,
                self.column_descending,
            )
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
            legacy,
        )

    def _build(self, builder: Any, name: str) -> PivotSpec:
        spec = self._layout(model=builder is not None)
        if builder is not None:
            if spec.legacy is None:
                spec.validate_model(builder)
            else:
                spec.legacy.validate(builder)
        else:
            if not isinstance(self.data, MemoryRows):
                raise ImproperlyConfigured("Declare a pivot source or data.")
            self.fields = self._memory_fields(self.data, spec)
            try:
                condition(
                    fixed_filters(self.filters),
                    set(self.fields).intersection(
                        self.fields if self.filter_fields is None else self.filter_fields
                    ),
                )
            except BuilderValidationError as exc:
                raise ImproperlyConfigured("Invalid fixed pivot filter.") from exc
        if spec.legacy is None:
            self.result_model = PivotMatrix
        return spec

    def _memory_fields(self, data: MemoryRows, spec: PivotSpec) -> tuple[str, ...]:
        columns = data.columns if data.columns or data.rows else spec.fields
        if not set(spec.fields).issubset(columns):
            raise ImproperlyConfigured("Pivot data is missing configured fields.")
        return columns

    def compute(
        self,
        data: Any = None,
        *,
        max_rows: int = 200,
        max_columns: int = 50,
        max_cells: int = 10_000,
    ) -> PivotMatrix:
        """Compute a matrix from records or a DataFrame without binding to an application."""
        spec = self._layout()
        source = self.data if data is None else MemoryRows.from_data(data, tuple(self.fields))
        if not isinstance(source, MemoryRows):
            raise ImproperlyConfigured("Provide records or a DataFrame to compute a pivot.")
        fields = self._memory_fields(source, spec)
        rows = source.matching(
            readable=set(fields),
            filter_fields=fields if self.filter_fields is None else self.filter_fields,
            fixed=fixed_filters(self.filters),
        )
        return PivotMatrix.model_validate(
            execute_memory(
                rows, spec, max_rows=max_rows, max_columns=max_columns, max_cells=max_cells
            )
        )

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
    code = "aggregate_timeout"


class Analytics:
    name = "analytics"
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
