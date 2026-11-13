"""Login, logout, account landing, and self-service profile screens."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from obox_builder.builder.http import OboxRoute, _authentication_challenge, read_form
from obox_builder.builder.service import invoke
from obox_builder.builder.ui import build_navigation
from obox_builder.errors import (
    N_,
    BuilderValidationError,
    ImproperlyConfigured,
    PermissionDenied,
)
from obox_builder.i18n import context as translation_context
from obox_builder.i18n import localize, translate

if TYPE_CHECKING:
    from obox_builder.application import Obox
    from obox_builder.context import Context
    from obox_builder.contrib.auth import Auth


def safe_redirect_url(request: Request, target: str) -> str | None:
    target = target.strip()
    if (
        not target
        or len(target) > 2048
        or "\\" in target
        or target.startswith("///")
        or any(ord(character) < 32 or ord(character) == 127 for character in target)
    ):
        return None
    try:
        url = urlsplit(target)
    except ValueError:
        return None
    if url.scheme and url.scheme.lower() not in {"http", "https"}:
        return None
    if url.scheme and not url.netloc:
        return None
    if url.netloc and url.netloc.casefold() != request.url.netloc.casefold():
        return None
    if request.url.scheme == "https" and url.scheme.lower() == "http":
        return None
    return target


def router(app: Obox, auth: Auth) -> APIRouter:
    routes = APIRouter(prefix=auth.prefix, route_class=OboxRoute)

    async def login(request: Request) -> Response:
        anonymous = auth.anonymous()
        error, status = "", 200
        redirect_to = request.query_params.get(auth.redirect_field_name, "")
        if request.method == "GET" and not request.cookies.get(auth.login_cookie):
            # Seed a cookie before issuing a CSRF token bound to it.
            next_url = redirect_to if auth.redirect_field_name in request.query_params else None
            response = RedirectResponse(auth.login_url(request, next_url=next_url), status_code=303)
            auth.set_cookie(response, auth.login_cookie, secrets.token_urlsafe(32), 3600)
            response.headers["cache-control"] = "no-store"
            return response
        if request.method == "POST":
            form = await read_form(request)
            redirect_to = str(form.get(auth.redirect_field_name, redirect_to))
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
                response = RedirectResponse(
                    safe_redirect_url(request, redirect_to) or auth.get_login_redirect_url(request),
                    status_code=303,
                )
                auth.set_cookie(response, auth.cookie_name, token, auth.max_age)
                auth.set_cookie(response, auth.login_cookie, "", 0)
                response.headers["cache-control"] = "no-store"
                return response
            error, status = N_("Invalid username or password."), 401
        rendered = app.templates.TemplateResponse(
            request=request,
            name=auth.login_template,
            context={
                **translation_context(request),
                "title": translate(request, "Sign in"),
                "site_title": app.app.title,
                "base_template": app.settings.base_template,
                "navigation": [],
                "error": error,
                "login_label": auth.login_label,
                "redirect_field_name": auth.redirect_field_name,
                "redirect_to": redirect_to,
                "csrf": app.csrf.token(request, anonymous),
            },
            status_code=status,
            headers={"cache-control": "no-store"},
        )
        if status == 401:
            rendered.headers["WWW-Authenticate"] = _authentication_challenge(request, auth)
        return rendered

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
                        localize(
                            request,
                            str(request.url_for(f"obox_ui_{builder.slug}_list").path),
                            rooted=True,
                        ),
                        status_code=303,
                    )
        return app.templates.TemplateResponse(
            request=request,
            name=auth.account_template,
            context={
                **translation_context(request),
                "title": translate(request, "Account"),
                "site_title": app.app.title,
                "base_template": app.settings.base_template,
                "navigation": [],
                "auth": auth,
                "principal": principal,
                "csrf": app.csrf.token(request, principal),
            },
            headers={"cache-control": "no-store"},
        )

    async def profile(request: Request) -> Response:
        principal = await auth.principal(request)
        if not principal.is_authenticated:
            return RedirectResponse(auth.login_url(request), status_code=303)
        backend = auth.backend
        if backend is None:
            raise ImproperlyConfigured("Install Auth before using its backend.")
        user_model = getattr(backend, "user_model", None)
        load_user = getattr(backend, "get_user", None)
        hash_password = getattr(backend, "hash_password", None)
        verify_password = getattr(backend, "verify_password", None)
        can_change_password = (
            user_model is not None and callable(hash_password) and callable(verify_password)
        )
        username_field = str(getattr(backend, "username_field", "username"))

        async def render(
            ctx: Context,
            user: Any | None,
            *,
            status: int = 200,
            errors: Mapping[str, list[str]] | None = None,
        ) -> HTMLResponse:
            def value(name: str) -> str:
                if user is None:
                    return ""
                return str(getattr(user, name, "") or "")

            return cast(
                HTMLResponse,
                app.templates.TemplateResponse(
                    request=request,
                    name="obox/profile.html",
                    context={
                        **translation_context(request),
                        "title": translate(request, "Account"),
                        "site_title": app.app.title,
                        "base_template": app.settings.base_template,
                        "navigation": await build_navigation(app, request, ctx),
                        "principal": ctx.principal,
                        "auth": auth,
                        "csrf": app.csrf.token(request, ctx.principal),
                        "profile": {
                            "username": value(username_field) or principal.username,
                            "email": value("email"),
                            "first_name": value("first_name"),
                            "last_name": value("last_name"),
                            "department": value("department"),
                            "is_active": bool(getattr(user, "is_active", principal.is_active)),
                            "is_superuser": bool(
                                getattr(
                                    user,
                                    "is_superuser",
                                    getattr(principal, "is_superuser", False),
                                )
                            ),
                            "roles": sorted(str(role) for role in getattr(principal, "roles", ())),
                        },
                        "can_change_password": can_change_password,
                        "errors": dict(errors or {}),
                        "saved": request.query_params.get("saved") == "1",
                        "profile_url": auth.profile_url(request),
                    },
                    status_code=status,
                    headers={"cache-control": "no-store"},
                ),
            )

        new_password = ""
        username = principal.username
        async with app.uow(principal=principal, request=request, source="ui") as ctx:
            user = None
            if user_model is not None:
                if callable(load_user):
                    user = await cast(Any, load_user)(ctx.session, principal.id, request=request)
                else:
                    user = await ctx.session.get(user_model, principal.id)
                if user is None:
                    return RedirectResponse(auth.login_url(request), status_code=303)

            if request.method == "GET":
                return await render(ctx, user)

            if not can_change_password:
                raise PermissionDenied(N_("Password changes are not supported by this backend."))
            form = await read_form(request)
            app.csrf.validate(request, principal, str(form.get("csrf_token", "")))
            if any(len(form.getlist(key)) != 1 for key in form):
                raise BuilderValidationError(
                    {"__all__": [N_("Duplicate form fields are not permitted.")]}
                )
            current_password = str(form.get("current_password", ""))
            new_password = str(form.get("password", ""))
            confirmation = str(form.get("confirm_password", ""))
            errors: dict[str, list[str]] = {}
            if not current_password:
                errors["current_password"] = [N_("This field is required.")]
            elif len(current_password) > 1024:
                errors["current_password"] = [N_("Use at most 1024 characters.")]
            elif not await cast(Any, verify_password)(
                current_password, getattr(user, "password_hash", None)
            ):
                errors["current_password"] = [N_("Current password is incorrect.")]
            if not new_password:
                errors["password"] = [N_("This field is required.")]
            elif new_password != confirmation:
                errors["confirm_password"] = [N_("Passwords do not match.")]
            encoded = None
            if new_password:
                try:
                    encoded = await cast(Any, hash_password)(new_password, user=user)
                except BuilderValidationError as error:
                    errors.setdefault("password", []).extend(error.errors.get("password", []))
            if errors:
                return await render(ctx, user, status=422, errors=errors)
            if encoded is None or user is None:
                raise PermissionDenied(N_("Password changes are not supported by this backend."))
            user.password_hash = encoded
            username = str(getattr(user, username_field, principal.username))

        token = await auth.login(
            username,
            new_password,
            previous=auth.get_token(request),
            request=request,
        )
        if token is None:
            response = RedirectResponse(auth.login_url(request), status_code=303)
            auth.set_cookie(response, auth.cookie_name, "", 0)
        else:
            response = RedirectResponse(f"{auth.profile_url(request)}?saved=1", status_code=303)
            auth.set_cookie(response, auth.cookie_name, token, auth.max_age)
        response.headers["cache-control"] = "no-store"
        return response

    routes.add_api_route(
        "/",
        account,
        methods=["GET"],
        name=auth.account_route,
        include_in_schema=False,
    )
    routes.add_api_route(
        "/profile",
        profile,
        methods=["GET", "POST"],
        name=auth.profile_route,
        include_in_schema=False,
    )
    routes.add_api_route(
        "/login", login, methods=["GET", "POST"], name=auth.login_route, include_in_schema=False
    )
    routes.add_api_route(
        "/logout", logout, methods=["POST"], name=auth.logout_route, include_in_schema=False
    )
    return routes
