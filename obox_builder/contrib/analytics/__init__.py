"""Code-defined dashboards over registered resource models."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy.exc import DBAPIError

from obox_builder.builder.aggregates import Aggregation, Measure
from obox_builder.builder.filters import FilterGroup, parse_filter
from obox_builder.contrib.analytics.charts import (
    Avg,
    Axis,
    Chart,
    ChartBuilder,
    Count,
    Dim,
    Field,
    Max,
    Min,
    Series,
    Sum,
    _terms,
    bar,
    donut,
    line,
    pie,
    scatter,
)
from obox_builder.errors import ImproperlyConfigured, NotFound, OboxError

if TYPE_CHECKING:
    from obox_builder.application import Obox
    from obox_builder.context import Context

__all__ = [
    "Analytics",
    "Avg",
    "Axis",
    "Chart",
    "ChartBuilder",
    "Count",
    "Dashboard",
    "Dim",
    "Field",
    "Group",
    "Max",
    "Measure",
    "Min",
    "Pivot",
    "Series",
    "Sum",
    "bar",
    "donut",
    "line",
    "pie",
    "scatter",
]


def _slug(value: str, *, widget: bool = False) -> None:
    pattern = r"[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)*" if widget else r"[a-z][a-z0-9_-]*"
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
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


@dataclass(frozen=True, init=False)
class Dashboard:
    slug: str
    title: str
    source: type[Any] | None
    widgets: tuple[Pivot | Chart, ...]
    groups: tuple[Group, ...]

    def __init__(
        self,
        slug: str,
        title: str,
        source: type[Any] | None = None,
        widgets: Sequence[Pivot | Chart | type[ChartBuilder]] = (),
        *,
        groups: Sequence[Group] = (),
    ) -> None:
        _slug(slug)
        normalized = []
        for widget in widgets:
            if isinstance(widget, type) and issubclass(widget, ChartBuilder):
                widget = widget.as_chart()
            if not isinstance(widget, (Pivot, Chart)):
                raise ImproperlyConfigured("Use Pivot, Chart or ChartBuilder declarations.")
            normalized.append(widget)
        for name, value in (
            ("slug", slug),
            ("title", title),
            ("source", source),
            ("widgets", tuple(normalized)),
            ("groups", tuple(groups)),
        ):
            object.__setattr__(self, name, value)
        if (
            not isinstance(self.title, str)
            or not self.title.strip()
            or not (self.widgets or self.groups)
        ):
            raise ImproperlyConfigured("Dashboards require a title and at least one widget.")
        if self.groups:
            if self.widgets or self.source is not None:
                raise ImproperlyConfigured(
                    "Declare grouped dashboard sources and widgets on groups."
                )
            if any(not isinstance(group, Group) or group.groups for group in self.groups) or len(
                {group.slug for group in self.groups}
            ) != len(self.groups):
                raise ImproperlyConfigured("Use unique source groups without nesting.")
        elif self.source is None and any(
            not isinstance(widget, Chart) or widget.source is None for widget in self.widgets
        ):
            raise ImproperlyConfigured("A dashboard or group requires a source.")
        seen = set()
        for widget in self.widgets:
            _slug(widget.slug, widget=True)
            key = (
                widget.source if self.source is None and isinstance(widget, Chart) else None,
                widget.slug,
            )
            if key in seen:
                raise ImproperlyConfigured("Widget slugs must be unique within a dashboard.")
            seen.add(key)
            _filters(widget.filters)
            if isinstance(widget, Chart):
                for series in widget.series:
                    if series.where is not None:
                        _filters(series.where)
            if isinstance(widget, Chart) and widget.kind not in {
                "bar",
                "line",
                "scatter",
                "pie",
                "donut",
            }:
                raise ImproperlyConfigured("Unsupported chart type.")

    @property
    def sections(self) -> tuple[Dashboard, ...]:
        return self.groups or (self,)

    @property
    def all_widgets(self) -> tuple[Pivot | Chart, ...]:
        return tuple(widget for section in self.sections for widget in section.widgets)


class Group(Dashboard):
    """A source and its widgets within a dashboard page."""


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
        dashboards: Dashboard | Sequence[Dashboard],
        *,
        prefix: str = "/analytics",
        max_rows: int = 200,
        max_columns: int = 50,
        max_cells: int = 10_000,
        max_chart_groups: int = 200,
        max_pie_groups: int = 50,
        max_points: int = 10_000,
        timeout_ms: int = 3000,
    ) -> None:
        self.dashboards = (dashboards,) if isinstance(dashboards, Dashboard) else tuple(dashboards)
        if not isinstance(prefix, str) or re.fullmatch(r"(?:/[a-z][a-z0-9_-]*)+", prefix) is None:
            raise ImproperlyConfigured("Analytics.prefix must be an absolute URL path.")
        self.prefix = prefix
        for _name, value in (
            ("max_rows", max_rows),
            ("max_columns", max_columns),
            ("max_cells", max_cells),
            ("max_chart_groups", max_chart_groups),
            ("max_pie_groups", max_pie_groups),
            ("max_points", max_points),
            ("timeout_ms", timeout_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ImproperlyConfigured("Analytics limits must be positive integers.")
        self.max_rows = max_rows
        self.max_columns = max_columns
        self.max_cells = max_cells
        self.max_chart_groups = max_chart_groups
        self.max_pie_groups = max_pie_groups
        self.max_points = max_points
        self.timeout_ms = timeout_ms

    def setup(self, app: Obox) -> None:
        from obox_builder.contrib.analytics.routes import build_router

        if hasattr(self, "app"):
            raise ImproperlyConfigured("Analytics is already installed.")
        seen = set()
        dashboards = []
        for dashboard in self.dashboards:
            if not isinstance(dashboard, Dashboard) or dashboard.slug in seen:
                raise ImproperlyConfigured("Use dashboards with unique slugs.")
            seen.add(dashboard.slug)
            if not dashboard.groups and dashboard.source is None:
                grouped: dict[type[Any], list[Chart]] = {}
                for widget in dashboard.widgets:
                    if isinstance(widget, Chart):
                        grouped.setdefault(_source(app, widget.source), []).append(widget)
                dashboard = replace(
                    dashboard,
                    widgets=(),
                    groups=tuple(
                        Group(
                            cast(str, app.get_builder(source).slug).lower(),
                            source.__name__,
                            source,
                            charts,
                        )
                        for source, charts in grouped.items()
                    ),
                )
            sections = []
            for section in dashboard.sections:
                source = _source(app, section.source)
                builder = app.get_builder(source)
                widgets = []
                for widget in section.widgets:
                    if isinstance(widget, Chart):
                        if widget.source is not None and _source(app, widget.source) is not source:
                            raise ImproperlyConfigured("A chart must use its group's source.")
                        widget.validate(builder)
                    else:
                        widget.aggregation.validate(builder)
                    from obox_builder.builder.service import _filter_condition

                    predicates = [widget.filters]
                    if isinstance(widget, Chart):
                        predicates.extend(
                            series.where for series in widget.series if series.where is not None
                        )
                    for predicate in predicates:
                        for term in _terms(predicate):
                            try:
                                _filter_condition(
                                    builder,
                                    set(builder.fields or ()),
                                    term.field,
                                    term.op,
                                    term.value,
                                )
                            except OboxError as exc:
                                raise ImproperlyConfigured("Invalid fixed widget filter.") from exc
                    widgets.append(
                        replace(widget, slug=f"{section.slug}.{widget.slug}")
                        if dashboard.groups
                        else widget
                    )
                sections.append(replace(section, source=source, widgets=tuple(widgets)))
            dashboard = (
                replace(
                    dashboard,
                    groups=tuple(
                        Group(section.slug, section.title, section.source, section.widgets)
                        for section in sections
                    ),
                )
                if dashboard.groups
                else sections[0]
            )
            dashboards.append(dashboard)
        self.dashboards = tuple(dashboards)
        self.app = app
        app.mount_static(
            self.prefix + "/_static",
            namespace="analytics",
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
        for section in dashboard.sections:
            service = self.app.service(_source(self.app, section.source))
            if not await service.permitted("list", ctx):
                continue
            fields = set(service.builder.readable(ctx)).intersection(
                service.builder.policy.aggregate_fields(ctx)
            )
            if any(
                set(
                    widget.fields if isinstance(widget, Chart) else widget.aggregation.fields
                ).issubset(fields)
                for widget in section.widgets
            ):
                return True
        return False


def _source(app: Obox, source: type[Any] | None) -> type[Any]:
    from obox_builder.builder import ModelBuilder

    if source is None:
        raise ImproperlyConfigured("A source group requires a registered resource.")

    model = (
        source._model_type()
        if isinstance(source, type) and issubclass(source, ModelBuilder)
        else source
    )
    if model is None:
        raise ImproperlyConfigured("Sources require a concrete registered model.")
    try:
        builder = app.get_builder(model)
    except (KeyError, TypeError) as exc:
        raise ImproperlyConfigured("Chart sources must be registered resources.") from exc
    if model is not source and not isinstance(builder, source):
        raise ImproperlyConfigured("Use the registered source builder.")
    return model
