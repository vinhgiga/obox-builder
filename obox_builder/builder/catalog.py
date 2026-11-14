"""Permission-filtered application homes and shared navigation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from starlette._utils import get_route_path

from obox_builder.builder.http import OboxRoute
from obox_builder.context import Context
from obox_builder.errors import NotAuthenticated, NotFound
from obox_builder.i18n import context as translation_context
from obox_builder.i18n import localize, translate

if TYPE_CHECKING:
    from obox_builder.application import Obox


async def app_list(obox: Obox, request: Request, ctx: Context) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not getattr(ctx.principal, "is_authenticated", False) or not getattr(
        ctx.principal, "is_active", False
    ):
        return result
    analytics: Any = obox.get_plugin("analytics")
    for config in obox.apps.values():
        entries = []
        for registration in obox._registrations.values():
            if registration.app_label != config.label:
                continue
            target, service = registration.builder, registration.service
            list_url = (
                localize(request, target.ui_path)
                if target.view.enabled and await service.permitted("list", ctx)
                else None
            )
            add_url = (
                localize(request, target.ui_path + "/new")
                if target.form.enabled
                and "create" in target.view.get_operations(ctx)
                and await service.permitted("create", ctx)
                else None
            )
            if list_url or add_url:
                entries.append(
                    {
                        "label": target.model.__name__ if target.model else registration.slug,
                        "url": list_url or add_url,
                        "list_url": list_url,
                        "add_url": add_url,
                        "kind": "model",
                    }
                )
        entries.extend(
            {
                "label": translate(request, page.title or page.name),
                "url": localize(
                    request, str(request.url_for(page.route_name(label)).path), rooted=True
                ),
                "kind": "page",
            }
            for (label, _), page in obox.pages.items()
            if label == config.label and page.menu and page.permitted(ctx.principal)
        )
        entries.extend(await config.get_navigation(obox, request, ctx))
        if analytics is not None:
            entries.extend(
                [
                    {
                        "label": translate(request, dashboard.title),
                        "url": localize(request, analytics.dashboard_path(dashboard)),
                        "kind": "dashboard",
                    }
                    for dashboard in analytics.dashboards
                    if dashboard.app_label == config.label
                    and await analytics.visible(dashboard, ctx)
                ]
            )
        if entries:
            result.append(
                {
                    "label": config.label,
                    "title": translate(request, config.verbose_name),
                    "url": localize(request, config.ui_path(obox.settings)),
                    "tab_bar_color": config.tab_bar_color,
                    "entries": entries,
                }
            )
    return result


async def navigation(obox: Obox, request: Request, ctx: Context) -> list[dict[str, Any]]:
    applications = await app_list(obox, request, ctx)
    path = localize(request, str(request.scope.get("obox_path", get_route_path(request.scope))))
    current = next(
        (
            item
            for item in applications
            if path == item["url"] or path.startswith(item["url"] + "/")
        ),
        None,
    )
    request.state.obox_catalog = {
        "home_url": localize(request, obox.settings.ui_prefix or "/"),
        "apps": applications,
        "current": current,
    }
    if current is None:
        return []
    return [
        {
            "label": translate(request, "Home"),
            "url": current["url"],
            "active": path == current["url"],
        },
        *(
            {**item, "active": path == item["url"] or path.startswith(item["url"] + "/")}
            for item in current["entries"]
        ),
    ]


def build_catalog(obox: Obox) -> APIRouter:
    router = APIRouter(route_class=OboxRoute, include_in_schema=False)
    dependency = Depends(obox.context_dependency("ui"), scope="function")

    async def render(request: Request, ctx: Context, app_label: str | None = None) -> HTMLResponse:
        if not getattr(ctx.principal, "is_authenticated", False) or not getattr(
            ctx.principal, "is_active", False
        ):
            raise NotAuthenticated("Authentication is required.")
        links = await navigation(obox, request, ctx)
        catalog = request.state.obox_catalog
        current = next((item for item in catalog["apps"] if item["label"] == app_label), None)
        if app_label is not None and current is None:
            raise NotFound("Application is unavailable.")
        return obox.templates.TemplateResponse(
            request=request,
            name="obox/app_index.html" if current else "obox/index.html",
            context={
                **translation_context(request),
                "title": current["title"] if current else translate(request, "Applications"),
                "site_title": obox.app.title,
                "base_template": obox.settings.base_template,
                "navigation": links,
                "applications": catalog["apps"],
                "application": current,
                "principal": ctx.principal,
                "auth": obox.auth,
                "csrf": obox.csrf.token(request, ctx.principal),
            },
            headers={"Cache-Control": "private, no-store"},
        )

    @router.get(obox.settings.ui_prefix or "/", name="obox_index")
    async def index(request: Request, ctx: Context = dependency) -> HTMLResponse:
        return await render(request, ctx)

    def app_home(label: str) -> Any:
        async def home(request: Request, ctx: Context = dependency) -> HTMLResponse:
            return await render(request, ctx, label)

        return home

    for config in obox.apps.values():
        router.add_api_route(
            config.ui_path(obox.settings),
            app_home(config.label),
            name=f"obox_app_{config.label}",
            response_class=HTMLResponse,
        )
    return router
