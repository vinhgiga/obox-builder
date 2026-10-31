"""Login and logout use the same CSRF protection as generated forms."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from obox_builder.builder.http import read_form
from obox_builder.builder.service import invoke
from obox_builder.errors import PermissionDenied

if TYPE_CHECKING:
    from obox_builder.application import Obox
    from obox_builder.contrib.auth import Auth


def router(app: Obox, auth: Auth) -> APIRouter:
    routes = APIRouter(prefix=auth.prefix)

    async def login(request: Request) -> Response:
        anonymous = auth.anonymous()
        error, status = "", 200
        if request.method == "GET" and not request.cookies.get(auth.login_cookie):
            # Seed a cookie before issuing a CSRF token bound to it.
            response = RedirectResponse(auth.login_url(request), status_code=303)
            auth.set_cookie(response, auth.login_cookie, secrets.token_urlsafe(32), 3600)
            response.headers["cache-control"] = "no-store"
            return response
        if request.method == "POST":
            form = await read_form(request)
            if not request.cookies.get(auth.login_cookie) or any(
                len(form.getlist(key)) != 1 for key in form
            ):
                raise PermissionDenied("Invalid login form.")
            app.csrf.validate(request, anonymous, str(form.get("csrf_token", "")))
            token = await auth.login(
                str(form.get("username", "")),
                str(form.get("password", "")),
                previous=auth.get_token(request),
                request=request,
            )
            if token:
                response = RedirectResponse(auth.account_url(request), status_code=303)
                auth.set_cookie(response, auth.cookie_name, token, auth.max_age)
                auth.set_cookie(response, auth.login_cookie, "", 0)
                response.headers["cache-control"] = "no-store"
                return response
            error, status = "Invalid username or password.", 401
        return app.templates.TemplateResponse(
            request=request,
            name=auth.login_template,
            context={
                "title": "Sign in",
                "site_title": app.title,
                "base_template": app.settings.base_template,
                "navigation": [],
                "error": error,
                "login_label": auth.login_label,
                "csrf": app.csrf.token(request, anonymous),
            },
            status_code=status,
            headers={"cache-control": "no-store"},
        )

    async def logout(request: Request) -> Response:
        principal = await auth.principal(request)
        form = await read_form(request)
        app.csrf.validate(request, principal, str(form.get("csrf_token", "")))
        await auth.logout(auth.get_token(request), request=request)
        response = RedirectResponse(auth.login_url(request), status_code=303)
        auth.set_cookie(response, auth.cookie_name, "", 0)
        response.headers["cache-control"] = "no-store"
        return response

    async def account(request: Request) -> Response:
        principal = await auth.principal(request)
        if not principal.is_authenticated:
            return RedirectResponse(auth.login_url(request), status_code=303)
        async with app.uow(principal=principal, request=request, source="ui") as ctx:
            for registration in app._registrations.values():
                builder = registration.builder
                if (
                    builder.view.enabled
                    and builder.policy is not None
                    and await invoke(builder.policy.allow, "list", ctx)
                ):
                    return RedirectResponse(
                        request.url_for(f"obox_ui_{builder.slug}_list"), status_code=303
                    )
        return app.templates.TemplateResponse(
            request=request,
            name=auth.account_template,
            context={
                "title": "Account",
                "site_title": app.title,
                "base_template": app.settings.base_template,
                "navigation": [],
                "auth": auth,
                "principal": principal,
                "csrf": app.csrf.token(request, principal),
            },
            headers={"cache-control": "no-store"},
        )

    routes.add_api_route(
        "/", account, methods=["GET"], name=auth.account_route, include_in_schema=False
    )
    routes.add_api_route(
        "/login", login, methods=["GET", "POST"], name=auth.login_route, include_in_schema=False
    )
    routes.add_api_route(
        "/logout", logout, methods=["POST"], name=auth.logout_route, include_in_schema=False
    )
    return routes
