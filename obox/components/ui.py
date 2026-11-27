"""HTML adapters for template-declared components."""

from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from jinja2 import pass_context
from markupsafe import Markup
from starlette.types import Receive, Scope, Send

from obox.components.declarations import ComponentQuery, ComponentRecords
from obox.components.scan import ComponentUse, component_signature
from obox.core.exceptions import BuilderValidationError, ImproperlyConfigured
from obox.db.filters import FilterGroup, FilterTerm, parse_filter
from obox.runtime.context import Context
from obox.web.dependencies import permission_required
from obox.web.i18n import context as translation_context
from obox.web.i18n import localize, translate
from obox.web.routes import ApiRoute
from obox.web.ui import build_navigation, filter_columns

if TYPE_CHECKING:
    from obox.components.service import ComponentService
    from obox.runtime.runtime import _Runtime

_rendering_request: ContextVar[Request | None] = ContextVar(
    "obox_template_component_request", default=None
)


@contextmanager
def use_component_request(request: Request) -> Any:
    token: Token[Request | None] = _rendering_request.set(request)
    try:
        yield
    finally:
        _rendering_request.reset(token)


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


def endpoint(service: ComponentService, action: str) -> str:
    return f"obox_component_{service.app_label}_{service.name}_{action}_ui"


def _query(request: Request, service: ComponentService, *, records: bool = False) -> ComponentQuery:
    obox = request.app.state.obox
    prefixes = tuple(
        name + "." for label, name in obox._template_components if label == service.app_label
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
    if service.dataset is not None:
        filters, operators, predicate, search = {}, {}, None, None
        for key, value in pairs:
            if key == "search":
                search = value
            elif key == "filter":
                predicate = parse_filter(value)
            elif key.startswith("filter."):
                filters[key[7:]] = value
            elif key.startswith("filter_op."):
                operators[key[10:]] = value
            else:
                raise BuilderValidationError({key: ["Unknown query parameter."]})
        if set(operators) - set(filters):
            raise BuilderValidationError({"filter": ["Filter operator has no value."]})
        terms: list[FilterGroup | FilterTerm] = [
            FilterTerm(name, operators.get(name, "is"), value)
            for name, value in filters.items()
            if value != "" or operators.get(name) in {"is_empty", "is_not_empty"}
        ]
        if predicate is not None:
            terms.append(predicate)
        query._options = {
            "filter_group": FilterGroup(children=tuple(terms)),
            "search": search,
        }
    elif pairs:
        raise BuilderValidationError({"query": ["This component has no source filters."]})
    return query


def build_component_ui(
    obox: _Runtime, app_label: str, services: Sequence[ComponentService]
) -> APIRouter:
    from obox.web.routes import OboxRoute

    router = APIRouter(
        route_class=OboxRoute,
        include_in_schema=False,
        dependencies=[Depends(permission_required(f"{app_label}.components.access"))],
    )
    dependency = Depends(obox.context_dependency("ui"), scope="function")
    bindings = tuple(services)
    if any(service.definition.plugin == "analytics" for service in bindings) and not any(
        getattr(route, "name", None) == "obox_analytics_components_static"
        for route in obox.app.routes
    ):
        obox.mount_static(
            f"{obox.settings.ui_prefix}/_components/static",
            namespace="analytics",
            name="obox_analytics_components_static",
        )

    def add(service: ComponentService) -> None:
        if service.query_model is None:
            return
        path = f"/_components/{service.name}"

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
            config = obox.apps[app_label]
            back = localize(request, config.ui_path(obox.settings))
            return cast(
                HTMLResponse,
                obox.templates.TemplateResponse(
                    request=request,
                    name="obox_analytics/records.html",
                    context={
                        **translation_context(request),
                        "request": request,
                        "base_template": obox.settings.base_template,
                        "title": translate(request, service.definition.title),
                        "site_title": obox.app.title,
                        "principal": ctx.principal,
                        "auth": obox.auth,
                        "csrf": obox.csrf.token(request, ctx.principal),
                        "navigation": await build_navigation(obox, request, ctx),
                        "columns": columns,
                        "rows": rows,
                        "back_url": back,
                        "previous": str(
                            base.include_query_params(offset=max(0, query.offset - query.limit))
                        )
                        if query.offset
                        else None,
                        "following": str(
                            base.include_query_params(offset=query.offset + query.limit)
                        )
                        if result.has_more
                        else None,
                    },
                ),
            )

        router.add_api_route(
            path + "/records",
            records,
            methods=["GET"],
            name=endpoint(service, "records"),
            route_class_override=ComponentDataRoute,
        )

    for service in bindings:
        add(service)
    return router


def install_component_global(env: Any) -> None:
    @pass_context
    def component(context: Any, ref: str, **kwargs: Any) -> Markup:
        key = kwargs.pop("key", None)
        props = kwargs
        request = context.get("request") or _rendering_request.get()
        if request is None:
            raise ImproperlyConfigured("component() requires a TemplateView route.")
        signature = component_signature(ref, props)
        components = getattr(request.state, "obox_component_markup", {})
        try:
            if key is not None:
                expected = request.state.obox_component_keys[signature]
                if expected != key:
                    raise ImproperlyConfigured(
                        f"component({ref!r}) was rendered with an unscanned key."
                    )
            # These strings were rendered through the configured autoescaping Jinja environment.
            return Markup(components[signature])  # noqa: S704
        except KeyError as exc:
            raise ImproperlyConfigured(
                f"Component {ref!r} was not declared in this route's templates."
            ) from exc

    env.globals["component"] = component


async def resolve_components(
    obox: _Runtime,
    app_label: str,
    route_name: str,
    request: Request,
    ctx: Context,
) -> dict[str, Any]:
    from obox.contrib.analytics.charts import ChartSpec

    bindings: Sequence[tuple[ComponentUse, ComponentService]] = obox._route_components[
        app_label, route_name
    ]
    markup: dict[str, str] = {}
    keys: dict[str, str] = {}
    widget_config: dict[str, Any] = {}
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

    for use, service in bindings:
        markup[use.signature] = ""
        keys[use.signature] = service.name
        if not await service.permitted(ctx):
            continue
        definition, spec = service.definition, service.spec
        item: dict[str, Any] = {
            "key": service.name,
            "title": translate(request, definition.title),
            "filters": [],
            "search_enabled": False,
            "prefix": service.name + ".",
        }
        if definition.template:
            values = await service.context(ctx=ctx)
            markup[use.signature] = obox.templates.env.get_template(definition.template).render(
                {
                    **values,
                    "request": request,
                    "principal": ctx.principal,
                    "title": item["title"],
                }
            )
            continue
        if definition.plugin != "analytics":
            raise ImproperlyConfigured(
                f"Component {use.ref!r} requires a template or analytics renderer."
            )
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
        chart = spec if isinstance(spec, ChartSpec) else None
        widget_config[service.name] = {
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
                request, str(request.url_for(endpoint(service, "records")).path), rooted=True
            )
            if description.get("drill", False)
            else "",
            "rowLabel": chart.dimension.label or chart.dimension.field if chart is not None else "",
            "rowTotals": description.get("rowTotals", True),
            "columnTotals": description.get("columnTotals", True),
        }
        module = obox.templates.env.get_template("obox_analytics/components.html").make_module(
            {
                **translation_context(request),
                "widget_config": widget_config,
                "labels": labels,
                "request": request,
            }
        )
        markup[use.signature] = str(cast(Any, module).widget(service.name, item))

    request.state.obox_component_markup = markup
    request.state.obox_component_keys = keys
    return {
        "components": markup,
        "widget_config": widget_config,
        "labels": labels,
        "component_assets": {"analytics": bool(widget_config)},
    }
