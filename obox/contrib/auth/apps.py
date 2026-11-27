"""Auth feature and its application registration."""

from __future__ import annotations

from functools import partial
from importlib import import_module
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Response
from fastapi.routing import iter_route_contexts
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette._utils import get_route_path
from starlette.routing import NoMatchFound

from obox.apps import AppConfig
from obox.contrib.auth.authorization import Authorization
from obox.contrib.auth.backends import Backend, ModelBackend, TokenBackend
from obox.contrib.auth.password_validation import get_password_validators
from obox.core.exceptions import ImproperlyConfigured
from obox.core.principal import ANONYMOUS, Identity
from obox.web.i18n import N_, localize, translate

if TYPE_CHECKING:
    from obox.runtime.context import Context
    from obox.runtime.runtime import _Runtime


class AuthConfig(AppConfig):
    name = "obox.contrib.auth"
    label = "auth"
    verbose_name = N_("Users and roles")

    async def get_navigation(
        self, obox: _Runtime, request: Request, ctx: Context
    ) -> list[dict[str, Any]]:
        auth = obox.auth
        if (
            auth is None
            or not auth._admin_installed
            or not getattr(ctx.principal, "is_superuser", False)
        ):
            return []
        return [
            {"label": translate(request, "Users"), "url": auth.users_url(request), "kind": "page"},
            {"label": translate(request, "Roles"), "url": auth.roles_url(request), "kind": "page"},
        ]

    def ready(self) -> None:
        from obox.conf import settings

        options = dict(getattr(settings, "AUTH", {}))
        options.setdefault("secure", not settings.DEBUG)
        self._service_factory = partial(Auth, **options)


class Auth:
    name = "auth"
    requires: tuple[str, ...] = ()
    login_route = "obox_login"
    redirect_field_name = "next"
    logout_route = "obox_logout"
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
        admin: bool = True,
        router: APIRouter | None = None,
        api: bool = True,
        api_tokens: bool = False,
    ) -> None:
        if max_age <= 0:
            raise ValueError("max_age must be positive.")
        if backend is None and session_factory is not None:
            backend = ModelBackend(session_factory)
        self._backend = backend
        self.api, self.api_tokens = api, api_tokens
        if api_tokens and not api:
            raise ImproperlyConfigured("API tokens require the Auth JSON API.")
        self.admin = admin
        self.router = router
        self._admin_installed = False
        self._installed = False
        self._authorization: Authorization | None = None
        self.secure, self.max_age = secure, max_age
        self.prefix = prefix.rstrip("/")
        if not self.prefix.startswith("/") or any(char in self.prefix for char in "{}?#"):
            raise ImproperlyConfigured("Auth prefix must be a nonempty absolute URL path.")
        self.login_template = login_template
        self.cookie_name = "__Host-obox_session" if secure else "obox_session"
        self.login_cookie = "__Host-obox_login" if secure else "obox_login"

    @property
    def backend(self) -> Backend:
        if self._backend is None:
            raise ImproperlyConfigured("Install Auth before using its backend.")
        return self._backend

    @property
    def login_label(self) -> str:
        field = getattr(self.backend, "username_field", "username")
        return N_("Username") if field == "username" else field.replace("_", " ").capitalize()

    def get_token(self, request: Request) -> str:
        return request.cookies.get(self.cookie_name, "")

    async def principal(self, request: Request) -> Identity:
        backend = self.backend
        if (
            not self.api_tokens
            or "authorization" not in request.headers
            or getattr(request.state, "obox_html_permission", False)
        ):
            return await backend.resolve(self.get_token(request), request=request)
        if not self._installed:
            raise ImproperlyConfigured("Install Auth before using API tokens.")
        path = request.scope.get("obox_path", get_route_path(request.scope))
        files = request.app.state.obox.feature("files")
        files_prefix = getattr(files, "prefix", None)
        file_api = isinstance(files_prefix, str) and (
            path == files_prefix or path.startswith(files_prefix + "/")
        )
        if (
            path == self._api_prefix
            or path.startswith(self._api_prefix + "/")
            or path == self._api_prefix + ":describe"
            or file_api
            or getattr(request.state, "obox_app_api", False)
        ):
            values = request.headers.getlist("authorization")
            parts = values[0].split() if len(values) == 1 else []
            if len(parts) != 2 or parts[0].lower() != "bearer":
                return ANONYMOUS
            token_backend = cast(TokenBackend, backend)
            identity = await token_backend.resolve_api_token(parts[1], request=request)
            if identity.is_authenticated:
                if getattr(identity, "api_scopes", None) is None:
                    raise ImproperlyConfigured(
                        "Token backends must return an identity with api_scopes."
                    )
                request.state.obox_bearer_principal = identity
            return identity
        return await backend.resolve(self.get_token(request), request=request)

    async def login(
        self, username: str, password: str, *, previous: str = "", request: Request | None = None
    ) -> str | None:
        return await self.backend.login(
            username, password, previous=previous, max_age=self.max_age, request=request
        )

    async def logout(self, token: str, *, request: Request | None = None) -> None:
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
        target: str | None = request.app.state.obox.settings.login_redirect_url
        if target is None:
            return self.profile_url(request) or localize(request, "/")
        try:
            return self._url(request, target)
        except NoMatchFound:
            return target

    def logout_url(self, request: Request) -> str:
        return self._url(request, self.logout_route)

    def profile_url(self, request: Request) -> str:
        try:
            return self._url(request, self.profile_route)
        except NoMatchFound:
            return ""

    def users_url(self, request: Request) -> str:
        return self._url(request, self.users_route)

    def roles_url(self, request: Request) -> str:
        return self._url(request, self.roles_route)

    @property
    def authorization(self) -> Authorization:
        if self._authorization is None:
            raise ImproperlyConfigured("Install Auth before managing roles.")
        return self._authorization

    def attach(self, app: _Runtime) -> None:
        if self._installed:
            raise ImproperlyConfigured("Auth is already installed.")
        backend = self._backend if self._backend is not None else ModelBackend(app.session_factory)
        if isinstance(backend, ModelBackend) and backend.password_validators is None:
            backend.password_validators = get_password_validators(
                app.settings.auth_password_validators
            )
        routes = self.router
        if routes is None:
            from obox.contrib.auth import views

            routes = views.router(app, self)
        public = {self.login_route, self.logout_route}
        if not public <= {route.name for route in iter_route_contexts(routes.routes)}:
            raise ImproperlyConfigured(
                "Auth routes must include the configured login and logout names."
            )
        self._backend = backend
        self._authorization = Authorization(app, self)
        self._api_prefix = app.settings.api_prefix
        if self.api_tokens:
            if not isinstance(backend, TokenBackend):
                raise ImproperlyConfigured("This auth backend does not support API tokens.")
            if isinstance(backend, ModelBackend):
                backend.configure_api_tokens()
        user_model = getattr(backend, "user_model", None) if self.admin else None
        mapper = inspect(user_model, raiseerr=False)
        if (
            mapper is not None
            and (roles := mapper.relationships.get("roles")) is not None
            and roles.secondary is not None
        ):
            from obox.contrib.auth.admin import router

            config = app.apps.get(self.name)
            if isinstance(config, AuthConfig):
                config.ui_prefix = self.prefix
            else:
                config = AuthConfig(AuthConfig.name, import_module(AuthConfig.name))
                config.ui_prefix = self.prefix
                app.add_app(config)
            app.include_router(
                router(app, self, mapper.class_, roles.mapper.class_),
                ui=True,
                permission="auth.manage_access",
            )
            self._admin_installed = True
        if self.router is None and app._enforce_permissions:
            for route in iter_route_contexts(routes.routes):
                if route.name in public:
                    app._public_routes.add(id(route.original_route))
        app.include_router(routes, ui=True)
        app.provide_defaults(principal_dependency=self.principal)
        if self.api:
            from obox.contrib.auth.api import build_router

            api_routes = build_router(app, self)
            if app._enforce_permissions:
                for route in iter_route_contexts(api_routes.routes):
                    if route.name in {
                        "obox_api_auth_csrf",
                        "obox_api_auth_login",
                        "obox_api_auth_logout",
                    }:
                        app._public_routes.add(id(route.original_route))
            app.include_router(api_routes)
        app.add_ui_prefix(self.prefix)
        self._installed = True
