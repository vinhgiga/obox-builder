"""Typed JSON contracts for analytical results."""

from typing import Literal

from pydantic import JsonValue

from obox._schemas import ApiResponse


class PivotData(ApiResponse):
    rows: list[JsonValue]
    columns: list[JsonValue]
    cells: list[list[JsonValue]]
    row_totals: list[JsonValue]
    column_totals: list[JsonValue]
    total: JsonValue
    records: int


class PivotMatrix(ApiResponse):
    row_fields: list[str]
    column_fields: list[str]
    values: list[str]
    values_axis: Literal["rows", "columns"]
    values_position: int
    show_row_subtotals: bool
    show_column_subtotals: bool
    show_row_totals: bool
    show_column_totals: bool
    rows: list[list[tuple[str, JsonValue]]]
    columns: list[list[tuple[str, JsonValue]]]
    cells: list[list[list[JsonValue]]]
    row_totals: list[list[JsonValue]]
    column_totals: list[list[JsonValue]]
    total: list[JsonValue]
    records: int


class Point(ApiResponse):
    x: JsonValue
    y: JsonValue
    record_count: int
    state: Literal["noFacts", "allNull", "value"]
    selector: str | None = None
    plot_y: str | None = None
    percentage: str | None = None


class SeriesData(ApiResponse):
    id: str
    group: JsonValue
    label: str
    axis: str
    kind: str
    value_type: str
    points: list[Point]


class ChartData(ApiResponse):
    mode: Literal["raw", "aggregate"]
    records: int
    series: list[SeriesData]
    categories: list[JsonValue]
    grouped: bool
    x_label: str
    x_type: str
    point_count: int
