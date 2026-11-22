"""Page-local adapters for component data and the built-in analytical renderer."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from starlette.types import Receive, Scope, Send

from obox._components import ComponentQuery, ComponentRecords
from obox._context import Context
from obox._exceptions import BuilderValidationError
from obox._filters import FilterGroup, FilterTerm
from obox._i18n import context as translation_context
from obox._i18n import localize, translate
from obox._page_routes import PageRoute
from obox._routes import ApiRoute
from obox._ui import build_navigation, filter_columns, query_options
from obox.contrib.analytics.charts import ChartSpec
from obox.contrib.analytics.pivot import PivotSpec

if TYPE_CHECKING:
    from obox._component_service import ComponentService
    from obox._pages import Page
    from obox._runtime import _Runtime


class ComponentDataRoute(ApiRoute):
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


def _query(request: Request, service: ComponentService, *, records: bool = False) -> ComponentQuery:
    prefixes = tuple(
        binding.name + "."
        for row in service.app._page_components[service.app_label, cast("Page", service.page).name]
        for binding in row
    )
    prefix = service.name + "."
    pairs, selection = [], {}
    for key, value in request.query_params.multi_items():
        if len(request.query_params.getlist(key)) != 1:
            raise BuilderValidationError({key: ["Supply this parameter once."]})
        if key in {"row", "column", "selector", "offset", "limit"}:
            if not records:
                raise BuilderValidationError({key: ["Unknown query parameter."]})
            try:
                selection[key] = json.loads(value) if key in {"row", "column"} else value
            except ValueError as exc:
                raise BuilderValidationError({key: ["Use a JSON selection key."]}) from exc
        elif key.startswith(prefix):
            pairs.append((key.removeprefix(prefix), value))
        elif not key.startswith(prefixes):
            raise BuilderValidationError({key: ["Use a declared component filter prefix."]})
    from pydantic import ValidationError

    try:
        query = ComponentRecords.model_validate(selection) if records else ComponentQuery()
    except ValidationError as exc:
        raise BuilderValidationError({"records": [error["msg"] for error in exc.errors()]}) from exc
    if service.dataset is not None or service.definition._supports_memory:
        values = query_options(
            Request(dict(request.scope, query_string=urlencode(pairs).encode())),
            service.dataset.dataset if service.dataset else None,
            pagination=False,
        )
        if "ordering" in values:
            raise BuilderValidationError(
                {"ordering": ["Component ordering is defined on the server."]}
            )
        for key in tuple(values["filters"]):
            if values["filters"][key] == "" and values["filter_ops"].get(key) not in {
                "is_empty",
                "is_not_empty",
            }:
                values["filters"].pop(key)
                values["filter_ops"].pop(key, None)
        if set(values["filter_ops"]) - set(values["filters"]):
            raise BuilderValidationError({"filter": ["Filter operator has no value."]})
        terms = [
            FilterTerm(name, values["filter_ops"].get(name, "is"), value)
            for name, value in values["filters"].items()
        ]
        if values["filter_group"] is not None:
            terms.append(values["filter_group"])
        query._options = {
            "filter_group": FilterGroup(children=tuple(terms)),
            "search": values["search"],
        }
    elif pairs:
        raise BuilderValidationError({"query": ["This component has no source filters."]})
    return query


def endpoint(service: ComponentService, action: str) -> str:
    return f"obox_component_{service.identity}_{action}_ui"


def build_page_components(obox: _Runtime, page: Page, app_label: str) -> APIRouter:
    router = APIRouter(route_class=PageRoute, include_in_schema=False)
    dependency = Depends(obox.context_dependency("ui"), scope="function")
    bindings = [service for row in obox._page_components[app_label, page.name] for service in row]
    if any(service.definition.plugin == "analytics" for service in bindings) and not any(
        getattr(route, "name", None) == "obox_analytics_static" for route in obox.app.routes
    ):
        obox.mount_static(
            f"{obox.settings.ui_prefix}/_components/static",
            namespace="analytics",
            name="obox_analytics_static",
        )

    def add(service: ComponentService) -> None:
        if service.query_model is None:
            return
        path = f"{page.path}/_components/{service.name}"

        async def data(request: Request, ctx: Context = dependency) -> Any:
            value = await service.query(_query(request, service), ctx=ctx)
            payload = value.model_dump(mode="json", by_alias=True)
            if service.definition.plugin == "analytics" and service.definition.kind != "pivot":
                payload["config"] = (await service.describe(ctx=ctx)).presentation
            return payload

        router.add_api_route(
            path + "/data",
            data,
            methods=["GET"],
            name=endpoint(service, "data"),
            route_class_override=ComponentDataRoute,
        )
        if not getattr(service.definition, "drill", False):
            return

        async def records(request: Request, ctx: Context = dependency) -> HTMLResponse:
            query = cast(ComponentRecords, _query(request, service, records=True))
            result = await service.records(query, ctx=ctx)
            columns = list(dict.fromkeys(key for record in result.records for key in record))
            rows = [
                {"values": [record.get(key) for key in columns], "url": None}
                for record in result.records
            ]
            base = request.url.remove_query_params("offset")
            return obox.templates.TemplateResponse(
                request=request,
                name="obox_analytics/records.html",
                context={
                    **translation_context(request),
                    "request": request,
                    "base_template": obox.settings.base_template,
                    "title": service.definition.title,
                    "site_title": obox.app.title,
                    "principal": ctx.principal,
                    "auth": obox.auth,
                    "csrf": obox.csrf.token(request, ctx.principal),
                    "navigation": await build_navigation(obox, request, ctx),
                    "columns": columns,
                    "rows": rows,
                    "back_url": localize(
                        request, str(request.url_for(page.route_name(app_label)).path), rooted=True
                    ),
                    "previous": str(
                        base.include_query_params(offset=max(0, query.offset - query.limit))
                    )
                    if query.offset
                    else None,
                    "following": str(base.include_query_params(offset=query.offset + query.limit))
                    if result.has_more
                    else None,
                },
            )

        router.add_api_route(
            path + "/records", records, methods=["GET"], name=endpoint(service, "records")
        )

    for binding in bindings:
        add(binding)
    return router


async def page_context(
    obox: _Runtime, page: Page, app_label: str, request: Request, ctx: Context
) -> dict[str, Any]:
    rows, config = [], {}
    labels = {
        "empty": translate(request, "Empty"),
        "total": translate(request, "Total"),
        "loading": translate(request, "Loading…"),
        "noData": translate(request, "No records found."),
        "error": translate(request, "Unable to load data. Try again."),
        "value": translate(request, "Value"),
        "series": translate(request, "Series"),
        "records": translate(request, "View records"),
        "chartOptions": translate(request, "Chart options"),
        "viewAsTable": translate(request, "View as table"),
        "viewAsChart": translate(request, "View as chart"),
        "expand": translate(request, "Expand"),
        "close": translate(request, "Close"),
        "allRecords": translate(request, "All records"),
    }
    for row in obox._page_components[app_label, page.name]:
        visible = []
        for service in row:
            if not await service.permitted(ctx):
                continue
            definition, spec = service.definition, service.spec
            item: dict[str, Any] = {
                "key": service.name,
                "title": translate(request, definition.title),
                "html": None,
                "filters": [],
                "search_enabled": False,
                "prefix": service.name + ".",
            }
            if definition.template:
                values = await service.context(ctx=ctx)
                item["html"] = obox.templates.env.get_template(definition.template).render(
                    {
                        **values,
                        **translation_context(request),
                        "request": request,
                        "principal": ctx.principal,
                        "title": item["title"],
                    }
                )
            else:
                description = (await service.describe(ctx=ctx)).presentation
                if service.dataset is not None:
                    builder = service.dataset.dataset
                    readable = set(builder.readable(ctx))
                    item.update(
                        filters=filter_columns(
                            builder,
                            ctx,
                            [name for name in builder.filter_fields if name in readable],
                        ),
                        search_enabled=any(name in readable for name in builder.search_fields),
                    )
                elif isinstance(spec, PivotSpec):
                    readable = set(definition.get_fields(ctx)).intersection(definition.fields)
                    fields = (
                        definition.fields
                        if definition.filter_fields is None
                        else definition.filter_fields
                    )
                    item.update(
                        filters=[
                            {
                                "name": name,
                                "label": name.replace("_", " ").capitalize(),
                                "kind": "text",
                                "choices": [],
                                "operators": [
                                    "is",
                                    "is_not",
                                    "contains",
                                    "does_not_contain",
                                    "is_empty",
                                    "is_not_empty",
                                ],
                            }
                            for name in fields
                            if name in readable
                        ],
                        search_enabled=bool(readable),
                    )
                chart = spec if isinstance(spec, ChartSpec) else None
                legacy = spec.legacy if isinstance(spec, PivotSpec) else None
                config[service.name] = {
                    "slug": service.name,
                    "title": item["title"],
                    "kind": definition.kind,
                    "description": definition.description,
                    "drill": description.get("drill", False),
                    "height": description.get("height", "md"),
                    "filterPrefix": item["prefix"],
                    "url": localize(
                        request, str(request.url_for(endpoint(service, "data")).path), rooted=True
                    ),
                    "recordsUrl": localize(
                        request,
                        str(request.url_for(endpoint(service, "records")).path),
                        rooted=True,
                    )
                    if description.get("drill", False)
                    else "",
                    "rowLabel": chart.dimension.label or chart.dimension.field
                    if chart is not None
                    else legacy.rows
                    if legacy
                    else "",
                    "columnLabel": (legacy.columns or "") if legacy else "",
                    "rowTotals": description.get("rowTotals", True),
                    "columnTotals": description.get("columnTotals", True),
                }
            visible.append(item)
        if visible:
            rows.append(visible)
    return {
        "component_rows": rows,
        "widget_config": config,
        "labels": labels,
        "analytics_assets": bool(config),
    }
