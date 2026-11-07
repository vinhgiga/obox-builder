"""Opt-in auth HTTP integration with replaceable backends, models and views."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette._utils import get_route_path
from starlette.routing import NoMatchFound

from obox_builder.auth import Identity, Principal, StaffOnly
from obox_builder.contrib.auth.backends import Backend, ModelBackend
from obox_builder.contrib.auth.models import (
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
    "Auth",
    "Backend",
    "Base",
    "ModelBackend",
    "Role",
    "RoleMixin",
    "Session",
    "SessionMixin",
    "User",
    "UserMixin",
    "hash_password",
]


class Auth:
    name = "auth"
    requires: tuple[str, ...] = ()
    login_route = "obox_login"
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
    ) -> None:
        if max_age <= 0:
            raise ValueError("max_age must be positive.")
        if backend is None and session_factory is not None:
            backend = ModelBackend(session_factory)
        self.backend = backend
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

    def login_url(self, request: Request) -> str:
        return self._url(request, self.login_route)

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
        app.add_ui_prefix(self.prefix)
        self._installed = True
