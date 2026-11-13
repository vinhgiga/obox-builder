"""Typed JSON contracts for dashboard discovery and analytical results."""

from typing import Literal

from pydantic import JsonValue

from obox_builder.builder.api_schema import ApiResponse


class FilterDescription(ApiResponse):
    name: str
    label: str
    kind: str
    choices: list[JsonValue]
    operators: list[str]


class SectionDescription(ApiResponse):
    slug: str
    title: str
    prefix: str
    filters: list[FilterDescription]
    search_enabled: bool
    explicit_rows: bool
    rows: list[list[str]]


class WidgetDescription(ApiResponse):
    slug: str
    title: str
    description: str
    kind: str
    drill: bool
    height: str
    filter_prefix: str
    url: str
    records_url: str
    row_label: str
    column_label: str
    row_totals: bool
    column_totals: bool


class Manifest(ApiResponse):
    slug: str
    title: str
    widgets: dict[str, WidgetDescription]
    labels: dict[str, str]
    sections: list[SectionDescription]
    assets: dict[str, str]


class PivotData(ApiResponse):
    rows: list[JsonValue]
    columns: list[JsonValue]
    cells: list[list[JsonValue]]
    row_totals: list[JsonValue]
    column_totals: list[JsonValue]
    total: JsonValue
    records: int


class AxisDescription(ApiResponse):
    label: str | None = None
    zero: bool | None = None
    minimum: float | None = None
    maximum: float | None = None


class ChartConfig(ApiResponse):
    kind: str
    orientation: str
    stack: str
    legend: str
    labels: bool
    percent: bool
    drill: bool
    primary_axis: AxisDescription
    secondary_axis: AxisDescription


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
    config: ChartConfig
