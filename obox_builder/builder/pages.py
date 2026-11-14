"""Page rendering through the shared UI context and authorization."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from starlette.datastructures import MutableHeaders
from starlette.types import Message, Receive, Scope, Send

from obox_builder.builder.http import OboxRoute
from obox_builder.builder.service import invoke
from obox_builder.builder.ui import build_navigation
from obox_builder.context import Context
from obox_builder.errors import ImproperlyConfigured, NotAuthenticated, PermissionDenied
from obox_builder.i18n import context as translation_context
from obox_builder.i18n import translate
from obox_builder.pages import Page

if TYPE_CHECKING:
    from obox_builder.application import Obox


class PageRoute(OboxRoute):
    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def respond(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "private, no-store"
                headers.add_vary_header("Cookie, Authorization, Accept-Language")
            await send(message)

        await super().handle(scope, receive, respond)


def build_page(obox: Obox, page: Page, app_label: str) -> APIRouter:
    router = APIRouter(route_class=PageRoute, include_in_schema=False)
    dependency = Depends(obox.context_dependency("ui"), scope="function")

    async def render(request: Request, ctx: Context = dependency) -> HTMLResponse:
        if not page.permitted(ctx.principal):
            if not getattr(ctx.principal, "is_authenticated", False) or not getattr(
                ctx.principal, "is_active", False
            ):
                raise NotAuthenticated("Authentication is required.")
            raise PermissionDenied("This page is not permitted.")
        values = await invoke(page.context, request, ctx) if page.context is not None else {}
        if not isinstance(values, Mapping):
            raise ImproperlyConfigured("Page context must return a mapping.")
        return cast(
            HTMLResponse,
            obox.templates.TemplateResponse(
                request=request,
                name=cast(str, page.template),
                context={
                    **values,
                    **translation_context(request),
                    "request": request,
                    "title": translate(request, cast(str, page.title)),
                    "site_title": obox.app.title,
                    "base_template": obox.settings.base_template,
                    "navigation": await build_navigation(obox, request, ctx),
                    "principal": ctx.principal,
                    "auth": obox.auth,
                    "csrf": obox.csrf.token(request, ctx.principal),
                },
            ),
        )

    router.add_api_route(
        cast(str, page.path), render, name=page.route_name(app_label), response_class=HTMLResponse
    )
    return router
