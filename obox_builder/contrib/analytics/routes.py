"""Read-only dashboard pages and bounded data requests."""

from __future__ import annotations

import json
import logging
from time import monotonic
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.exc import DBAPIError
from starlette.datastructures import MutableHeaders
from starlette.types import Message, Receive, Scope, Send

from obox_builder.builder.aggregates import encode
from obox_builder.builder.api import query_options
from obox_builder.builder.filters import FilterGroup, FilterTerm
from obox_builder.builder.http import ApiRoute, OboxRoute
from obox_builder.builder.ui import build_navigation, filter_columns, list_columns
from obox_builder.context import Context
from obox_builder.contrib.analytics import AggregateTimeout, Chart, Pivot, is_timeout
from obox_builder.errors import BuilderValidationError, NotFound
from obox_builder.i18n import context as translation_context
from obox_builder.i18n import localize

if TYPE_CHECKING:
    from obox_builder.contrib.analytics import Analytics

logger = logging.getLogger(__name__)
HEADERS = {"Cache-Control": "private, no-store"}


class AnalyticsRoute(OboxRoute):
    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def private(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).update(HEADERS)
            await send(message)

        await super().handle(scope, receive, private)


class AnalyticsDataRoute(ApiRoute):
    cache_control = "private, no-store"

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        language = scope.get("state", {}).get("language")
        if language:
            scope = dict(
                scope,
                headers=[
                    (key, value) for key, value in scope["headers"] if key != b"accept-language"
                ]
                + [(b"accept-language", language.encode())],
            )
        await super().handle(scope, receive, send)


def options(request: Request, builder: Any, widget: Pivot | Chart | None = None) -> dict[str, Any]:
    pairs = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key not in {"row", "column", "offset", "limit"}
    ]
    if any(key == "ordering" for key, _ in pairs):
        raise BuilderValidationError({"ordering": ["Widget ordering is defined on the server."]})
    scope = dict(request.scope, query_string=urlencode(pairs).encode())
    values = query_options(Request(scope), builder, pagination=False)
    empty = [
        name
        for name, value in values["filters"].items()
        if value == "" and values["filter_ops"].get(name) not in {"is_empty", "is_not_empty"}
    ]
    for name in empty:
        values["filters"].pop(name)
        values["filter_ops"].pop(name, None)
    terms: list[FilterTerm | FilterGroup] = [
        FilterTerm(name, values["filter_ops"].get(name, "is"), value)
        for name, value in values["filters"].items()
    ]
    if set(values["filter_ops"]) - set(values["filters"]):
        raise BuilderValidationError({"filter": ["Filter operator has no value."]})
    terms.extend(
        group
        for group in (widget.filters if widget else None, values["filter_group"])
        if group and group.children
    )
    return {"filter_group": FilterGroup(children=tuple(terms)), "search": values["search"]}


def validate_parameters(request: Request, *, records: bool = False) -> None:
    for key in request.query_params:
        if key in {"row", "column", "offset", "limit"} and not records:
            raise BuilderValidationError({key: ["Unknown query parameter."]})
        if len(request.query_params.getlist(key)) != 1:
            raise BuilderValidationError({key: ["Supply this parameter once."]})


def build_router(analytics: Analytics) -> APIRouter:
    app = analytics.app
    router = APIRouter(prefix=analytics.prefix, route_class=AnalyticsRoute, include_in_schema=False)
    dependency = Depends(app.context_dependency("ui"), scope="function")

    def resolve(ctx: Context, dashboard: str, widget: str | None = None) -> tuple[Any, Any, Any]:
        if getattr(ctx.principal, "api_scopes", None) is not None:
            ctx.source = "api"
        definition = analytics.dashboard(dashboard)
        selected = next((w for w in definition.widgets if w.slug == widget), None)
        if widget is not None and selected is None:
            raise NotFound("Unknown widget.")
        return definition, selected, app.service(definition.source)

    async def page(template: str, request: Request, ctx: Context, **values: Any) -> HTMLResponse:
        return cast(
            HTMLResponse,
            app.templates.TemplateResponse(
                request=request,
                name=template,
                context={
                    "request": request,
                    "base_template": app.settings.base_template,
                    "navigation": await build_navigation(app, request, ctx),
                    "principal": ctx.principal,
                    "site_title": app.app.title,
                    "auth": app.auth,
                    "csrf": app.csrf.token(request, ctx.principal),
                    **translation_context(request),
                    **values,
                },
                headers=HEADERS,
            ),
        )

    async def timeout(ctx: Context) -> None:
        if ctx.session.get_bind().dialect.name == "postgresql":
            await ctx.session.execute(
                sa.select(sa.func.set_config("statement_timeout", str(analytics.timeout_ms), True))
            )

    @router.get("/{dashboard}", name="obox_analytics_dashboard")
    async def dashboard_page(request: Request, dashboard: str, ctx: Context = dependency) -> Any:
        definition, _, service = resolve(ctx, dashboard)
        await service.authorize("list", ctx)
        validate_parameters(request)
        values = options(request, service.builder)
        await service._list_statement(ctx=ctx, **values)
        filters = [
            name
            for name in service.builder.view.get_list_filter(ctx)
            if name in service.builder.filter_fields
            and name in service.builder.readable(ctx)
            and "obox_files" not in service.builder.specs[name].column.info
        ]
        return await page(
            "obox_analytics/dashboard.html",
            request,
            ctx,
            title=definition.title,
            dashboard=definition,
            widgets=definition.widgets,
            filters=filter_columns(service.builder, ctx, filters),
            search_enabled=bool(service.builder.search_fields),
            dashboard_url=localize(request, analytics.prefix + "/" + definition.slug),
            widget_urls={
                widget.slug: {
                    endpoint: localize(
                        request,
                        str(
                            request.url_for(
                                "obox_analytics_" + endpoint,
                                dashboard=definition.slug,
                                widget=widget.slug,
                            ).path
                        ),
                        rooted=True,
                    )
                    for endpoint in ("data", "records")
                }
                for widget in definition.widgets
            },
        )

    async def data(request: Request, dashboard: str, widget: str, ctx: Context = dependency) -> Any:
        definition, selected, service = resolve(ctx, dashboard, widget)
        validate_parameters(request)
        started, groups, outcome = monotonic(), 0, "error"
        try:
            await timeout(ctx)
            limit = analytics.max_rows
            if isinstance(selected, Chart):
                limit = (
                    analytics.max_pie_groups
                    if selected.kind in {"pie", "donut"}
                    else (analytics.max_chart_groups)
                )
            result = await service.aggregate(
                selected.aggregation,
                ctx=ctx,
                **options(request, service.builder, selected),
                max_rows=limit,
                max_columns=analytics.max_columns,
                max_cells=analytics.max_cells,
            )
            groups = len(result.rows) * len(result.columns)
            if (
                isinstance(selected, Chart)
                and selected.kind in {"pie", "donut"}
                and any(value is not None and value < 0 for value in result.row_totals)
            ):
                raise BuilderValidationError(
                    {"chart": ["Negative values require a bar chart."]}, code="negative_chart"
                )
            outcome = "ok"
            return JSONResponse(result.to_dict(), headers=HEADERS)
        except DBAPIError as exc:
            if not is_timeout(exc):
                raise
            outcome = "timeout"
            raise AggregateTimeout("Query timed out. Narrow the filters and try again.") from exc
        except BuilderValidationError as exc:
            outcome = exc.code
            raise
        finally:
            logger.info(
                "analytics dashboard=%s widget=%s duration_ms=%.1f groups=%s outcome=%s",
                definition.slug,
                selected.slug,
                (monotonic() - started) * 1000,
                groups,
                outcome,
            )

    @router.get("/{dashboard}/{widget}/records", name="obox_analytics_records")
    async def records(
        request: Request,
        dashboard: str,
        widget: str,
        ctx: Context = dependency,
    ) -> Any:
        definition, selected, service = resolve(ctx, dashboard, widget)
        validate_parameters(request, records=True)
        keys = {}
        for parameter, name in (
            ("row", selected.aggregation.rows),
            ("column", selected.aggregation.columns),
        ):
            if parameter not in request.query_params:
                continue
            try:
                raw = request.query_params[parameter]
                if not name or len(raw) > 32_000:
                    raise ValueError
                keys[name] = json.loads(raw)
            except (ValueError, RecursionError) as exc:
                raise BuilderValidationError({parameter: ["Use a JSON group key."]}) from exc
        try:
            offset = int(request.query_params.get("offset", "0"))
            limit = int(request.query_params.get("limit", "50"))
        except ValueError as exc:
            raise BuilderValidationError({"pagination": ["Use integer offset and limit."]}) from exc
        try:
            await timeout(ctx)
            objects = await service.aggregate_records(
                selected.aggregation,
                ctx=ctx,
                keys=keys,
                offset=offset,
                limit=limit,
                **options(request, service.builder, selected),
            )
            builder = service.builder
            columns = [
                name
                for name in list_columns(builder, ctx, builder.view.get_list_display(ctx))
                if name in builder.specs
            ]
            rows = []
            for obj in objects:
                values = await service.serialize(obj, ctx=ctx, fields=columns)
                link = None
                if builder.view.enabled and await service.permitted("retrieve", ctx, obj):
                    link = localize(
                        request,
                        str(
                            request.url_for(
                                f"obox_ui_{builder.slug}_detail",
                                id=str(getattr(obj, builder.pk_name)),
                            ).path
                        ),
                        rooted=True,
                    )
                rows.append({"values": [encode(values.get(name)) for name in columns], "url": link})
        except DBAPIError as exc:
            if not is_timeout(exc):
                raise
            raise AggregateTimeout("Query timed out. Narrow the filters and try again.") from exc
        base = request.url.remove_query_params("offset")
        filters = urlencode(
            [
                (key, value)
                for key, value in request.query_params.multi_items()
                if key not in {"row", "column", "offset", "limit"}
            ]
        )
        back = localize(request, analytics.prefix + "/" + definition.slug)
        return await page(
            "obox_analytics/records.html",
            request,
            ctx,
            title=selected.title or selected.slug,
            columns=columns,
            rows=rows,
            previous=str(base.include_query_params(offset=max(0, offset - limit)))
            if offset
            else None,
            following=str(base.include_query_params(offset=offset + limit))
            if len(rows) == limit
            else None,
            dashboard_url=back + ("?" + filters if filters else ""),
        )

    router.add_api_route(
        "/{dashboard}/{widget}/data",
        data,
        methods=["GET"],
        name="obox_analytics_data",
        route_class_override=AnalyticsDataRoute,
    )
    return router
