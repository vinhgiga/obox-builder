"""Opt-in auth HTTP integration with replaceable backends, models and views."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette._utils import get_route_path
from starlette.routing import NoMatchFound

from obox_builder.auth import Identity, Principal, StaffOnly
from obox_builder.contrib.auth.backends import Backend, ModelBackend, TokenBackend
from obox_builder.contrib.auth.models import (
    APIToken,
    APITokenMixin,
    Base,
    Role,
    RoleMixin,
    Session,
    SessionMixin,
    User,
    UserMixin,
)
from obox_builder.contrib.auth.password_validation import get_password_validators
from obox_builder.contrib.auth.passwords import hash_password
from obox_builder.errors import N_, ImproperlyConfigured
from obox_builder.i18n import localize

if TYPE_CHECKING:
    from obox_builder.application import Obox

__all__ = [
    "APIToken",
    "APITokenMixin",
    "Auth",
    "Backend",
    "Base",
    "ModelBackend",
    "Role",
    "RoleMixin",
    "Session",
    "SessionMixin",
    "TokenBackend",
    "User",
    "UserMixin",
    "hash_password",
]


class Auth:
    name = "auth"
    requires: tuple[str, ...] = ()
    login_route = "obox_login"
    redirect_field_name = "next"
    logout_route = "obox_logout"
    account_route = "obox_account"
    profile_route = "obox_profile"
    users_route = "obox_auth_users"
    roles_route = "obox_auth_roles"

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        *,
        backend: Backend | None = None,
        secure: bool = True,
        max_age: int = 14 * 24 * 60 * 60,
        prefix: str = "/auth",
        login_template: str = "obox/login.html",
        account_template: str = "obox/account.html",
        admin: bool = True,
        router: APIRouter | None = None,
        api: bool = True,
        api_tokens: bool = False,
    ) -> None:
        if max_age <= 0:
            raise ValueError("max_age must be positive.")
        if backend is None and session_factory is not None:
            backend = ModelBackend(session_factory)
        self.backend = backend
        self.api, self.api_tokens = api, api_tokens
        if api_tokens and not api:
            raise ImproperlyConfigured("API tokens require the Auth JSON API.")
        self.admin = admin
        self.router = router
        self._admin_installed = False
        self.secure, self.max_age = secure, max_age
        self.prefix = prefix.rstrip("/")
        if not self.prefix.startswith("/") or any(char in self.prefix for char in "{}?#"):
            raise ImproperlyConfigured("Auth prefix must be a nonempty absolute URL path.")
        self.login_template, self.account_template = login_template, account_template
        field = getattr(backend, "username_field", "username")
        self.login_label = (
            N_("Username") if field == "username" else field.replace("_", " ").capitalize()
        )
        self.cookie_name = "__Host-obox_session" if secure else "obox_session"
        self.login_cookie = "__Host-obox_login" if secure else "obox_login"

    def anonymous(self) -> Identity:
        return Principal()

    def get_token(self, request: Request) -> str:
        return request.cookies.get(self.cookie_name, "")

    async def principal(self, request: Request) -> Identity:
        if self.backend is None:
            raise ImproperlyConfigured("Install Auth before using its backend.")
        if not self.api_tokens or "authorization" not in request.headers:
            return await self.backend.resolve(self.get_token(request), request=request)
        if not getattr(self, "_installed", False):
            raise ImproperlyConfigured("Install Auth before using API tokens.")
        path = request.scope.get("obox_path", get_route_path(request.scope))
        files = request.app.state.obox.get_plugin("files")
        files_prefix = getattr(files, "prefix", None)
        file_api = isinstance(files_prefix, str) and (
            path == files_prefix or path.startswith(files_prefix + "/")
        )
        if (
            path == self._api_prefix
            or path.startswith(self._api_prefix + "/")
            or path == self._api_prefix + ":describe"
            or file_api
        ):
            values = request.headers.getlist("authorization")
            parts = values[0].split() if len(values) == 1 else []
            if len(parts) != 2 or parts[0].lower() != "bearer":
                return self.anonymous()
            identity = await cast(TokenBackend, self.backend).resolve_api_token(
                parts[1], request=request
            )
            if identity.is_authenticated and getattr(identity, "api_scopes", None) is None:
                raise ImproperlyConfigured(
                    "Token backends must return an identity with api_scopes."
                )
            return identity
        return await self.backend.resolve(self.get_token(request), request=request)

    async def login(
        self, username: str, password: str, *, previous: str = "", request: Request | None = None
    ) -> str | None:
        if self.backend is None:
            raise ImproperlyConfigured("Install Auth before using its backend.")
        return await self.backend.login(
            username, password, previous=previous, max_age=self.max_age, request=request
        )

    async def logout(self, token: str, *, request: Request | None = None) -> None:
        if self.backend is None:
            raise ImproperlyConfigured("Install Auth before using its backend.")
        await self.backend.logout(token, request=request)

    def set_cookie(self, response: Response, name: str, value: str, max_age: int) -> None:
        response.set_cookie(
            name, value, max_age=max_age, secure=self.secure, httponly=True, samesite="lax"
        )

    def _url(self, request: Request, route: str) -> str:
        path = str(request.url_for(route).path)
        target = get_route_path({**request.scope, "path": path})
        if any(
            target == prefix or target.startswith(prefix + "/")
            for prefix in request.app.state.obox.ui_prefixes
        ):
            return localize(request, path, rooted=True)
        return path

    def login_url(self, request: Request, *, next_url: str | None = None) -> str:
        url = self._url(request, self.login_route)
        if next_url is not None:
            return f"{url}?{urlencode({self.redirect_field_name: next_url})}"
        return url

    def get_login_redirect_url(self, request: Request) -> str:
        target = cast(str, request.app.state.obox.settings.login_redirect_url)
        try:
            return self._url(request, target)
        except NoMatchFound:
            return target

    def logout_url(self, request: Request) -> str:
        return self._url(request, self.logout_route)

    def account_url(self, request: Request) -> str:
        return self._url(request, self.account_route)

    def profile_url(self, request: Request) -> str:
        for route in (self.profile_route, self.account_route):
            try:
                return self._url(request, route)
            except NoMatchFound:
                continue
        return ""

    def users_url(self, request: Request) -> str:
        return self._url(request, self.users_route)

    def roles_url(self, request: Request) -> str:
        return self._url(request, self.roles_route)

    def get_router(self, app: Obox) -> APIRouter:
        from obox_builder.contrib.auth.views import router

        return router(app, self)

    def get_admin_router(self, app: Obox) -> APIRouter:
        from obox_builder.contrib.auth.admin import router

        return router(app, self)

    def setup(self, app: Obox) -> None:
        if getattr(self, "_installed", False):
            raise ImproperlyConfigured("Auth is already installed.")
        backend = self.backend if self.backend is not None else ModelBackend(app.session_factory)
        if isinstance(backend, ModelBackend) and backend.password_validators is None:
            backend.password_validators = get_password_validators(
                app.settings.auth_password_validators
            )
        routes = self.get_router(app) if self.router is None else self.router
        names = {getattr(route, "name", None) for route in routes.routes}
        if not {self.login_route, self.logout_route} <= names:
            raise ImproperlyConfigured(
                "Auth routes must include the configured login and logout names."
            )
        self.backend = backend
        self._api_prefix = app.settings.api_prefix
        if self.api_tokens:
            if not isinstance(backend, TokenBackend):
                raise ImproperlyConfigured("This auth backend does not support API tokens.")
            if isinstance(backend, ModelBackend):
                backend.configure_api_tokens()
        user_model = getattr(backend, "user_model", None)
        if self.admin and user_model is not None:
            from sqlalchemy import inspect

            if "roles" in inspect(user_model).relationships:
                app.include_router(self.get_admin_router(app), ui=True)
                self._admin_installed = True
        app.include_router(routes, ui=True)
        app.provide_defaults(
            principal_dependency=self.principal,
            default_policy=StaffOnly(),
        )
        if self.api:
            from obox_builder.contrib.auth.api import build_router

            app.include_router(build_router(app, self))
        app.add_ui_prefix(self.prefix)
        self._installed = True
