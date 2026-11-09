"""Code-defined dashboards over registered resource models."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy.exc import DBAPIError

from obox_builder.builder.aggregates import Aggregation, Measure
from obox_builder.builder.filters import FilterGroup, parse_filter
from obox_builder.errors import ImproperlyConfigured, NotFound, OboxError

if TYPE_CHECKING:
    from obox_builder.application import Obox
    from obox_builder.context import Context

__all__ = ["Analytics", "Chart", "Dashboard", "Measure", "Pivot"]


def _slug(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_-]*", value) is None:
        raise ImproperlyConfigured("Use a lowercase slug beginning with a letter.")


def _filters(value: FilterGroup) -> None:
    import json

    if not isinstance(value, FilterGroup):
        raise ImproperlyConfigured("Widget filters require a FilterGroup.")
    try:
        parse_filter(json.dumps(value.to_dict()))
    except (OboxError, TypeError, ValueError, RecursionError) as exc:
        raise ImproperlyConfigured("Invalid fixed widget filters.") from exc


@dataclass(frozen=True)
class Pivot:
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


@dataclass(frozen=True)
class Chart:
    slug: str
    kind: Literal["bar", "line", "pie", "donut"]
    x: str
    measure: Measure = field(default_factory=Measure)
    title: str = ""
    filters: FilterGroup = field(default_factory=FilterGroup)
    sort: Literal["group", "value"] = "group"
    descending: bool = False

    @property
    def aggregation(self) -> Aggregation:
        return Aggregation(
            self.x,
            self.measure,
            row_sort="group" if self.kind == "line" else self.sort,
            row_descending=False if self.kind == "line" else self.descending,
        )


@dataclass(frozen=True)
class Dashboard:
    slug: str
    title: str
    source: type[Any]
    widgets: tuple[Pivot | Chart, ...]

    def __post_init__(self) -> None:
        _slug(self.slug)
        object.__setattr__(self, "widgets", tuple(self.widgets))
        if not isinstance(self.title, str) or not self.title.strip() or not self.widgets:
            raise ImproperlyConfigured("Dashboards require a title and at least one widget.")
        seen = set()
        for widget in self.widgets:
            if not isinstance(widget, (Pivot, Chart)):
                raise ImproperlyConfigured("Use Pivot or Chart widgets.")
            _slug(widget.slug)
            if widget.slug in seen:
                raise ImproperlyConfigured("Widget slugs must be unique within a dashboard.")
            seen.add(widget.slug)
            _filters(widget.filters)
            if isinstance(widget, Chart) and widget.kind not in {"bar", "line", "pie", "donut"}:
                raise ImproperlyConfigured("Unsupported chart type.")


class AggregateTimeout(OboxError):
    status = 503
    code = "aggregate_timeout"


def is_timeout(exc: DBAPIError) -> bool:
    original = exc.orig
    return any(
        getattr(error, "sqlstate", getattr(error, "pgcode", None)) == "57014"
        for error in (original, getattr(original, "__cause__", None))
    )


class Analytics:
    name = "analytics"
    requires: tuple[str, ...] = ()

    def __init__(
        self,
        dashboards: Sequence[Dashboard],
        *,
        prefix: str = "/analytics",
        max_rows: int = 200,
        max_columns: int = 50,
        max_cells: int = 10_000,
        max_chart_groups: int = 200,
        max_pie_groups: int = 50,
        timeout_ms: int = 3000,
    ) -> None:
        self.dashboards = tuple(dashboards)
        if not isinstance(prefix, str) or re.fullmatch(r"(?:/[a-z][a-z0-9_-]*)+", prefix) is None:
            raise ImproperlyConfigured("Analytics.prefix must be an absolute URL path.")
        self.prefix = prefix
        for _name, value in (
            ("max_rows", max_rows),
            ("max_columns", max_columns),
            ("max_cells", max_cells),
            ("max_chart_groups", max_chart_groups),
            ("max_pie_groups", max_pie_groups),
            ("timeout_ms", timeout_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ImproperlyConfigured("Analytics limits must be positive integers.")
        self.max_rows = max_rows
        self.max_columns = max_columns
        self.max_cells = max_cells
        self.max_chart_groups = max_chart_groups
        self.max_pie_groups = max_pie_groups
        self.timeout_ms = timeout_ms

    def setup(self, app: Obox) -> None:
        from fastapi.staticfiles import StaticFiles

        from obox_builder.contrib.analytics.routes import build_router

        if hasattr(self, "app"):
            raise ImproperlyConfigured("Analytics is already installed.")
        seen = set()
        for dashboard in self.dashboards:
            if not isinstance(dashboard, Dashboard) or dashboard.slug in seen:
                raise ImproperlyConfigured("Use dashboards with unique slugs.")
            seen.add(dashboard.slug)
            builder = app.get_builder(dashboard.source)
            for widget in dashboard.widgets:
                widget.aggregation.validate(builder)
                from obox_builder.builder.service import _filter_condition

                for term in _terms(widget.filters):
                    try:
                        _filter_condition(
                            builder, set(builder.fields or ()), term.field, term.op, term.value
                        )
                    except OboxError as exc:
                        raise ImproperlyConfigured("Invalid fixed widget filter.") from exc
        self.app = app
        app.app.mount(
            self.prefix + "/_static",
            StaticFiles(
                directory=Path(__file__).parent / "static",
                packages=[("obox_builder.builder", "static")],
            ),
            name="obox_analytics_static",
        )
        app.include_router(build_router(self), ui=True)
        app.add_ui_prefix(self.prefix)

    def dashboard(self, slug: str) -> Dashboard:
        for dashboard in self.dashboards:
            if dashboard.slug == slug:
                return dashboard
        raise NotFound("Unknown dashboard.")

    async def visible(self, dashboard: Dashboard, ctx: Context) -> bool:
        service = self.app.service(dashboard.source)
        if not await service.permitted("list", ctx):
            return False
        fields = set(service.builder.readable(ctx)).intersection(
            service.builder.policy.aggregate_fields(ctx)
        )
        return any(set(widget.aggregation.fields).issubset(fields) for widget in dashboard.widgets)


def _terms(group: FilterGroup) -> Any:
    from obox_builder.builder.filters import FilterTerm

    for child in group.children:
        if isinstance(child, FilterTerm):
            yield child
        else:
            yield from _terms(child)
