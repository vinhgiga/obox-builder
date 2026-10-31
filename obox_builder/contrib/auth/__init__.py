"""Opt-in auth HTTP integration with replaceable backends, models and views."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obox_builder.auth import Identity, Principal
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
from obox_builder.contrib.auth.passwords import hash_password
from obox_builder.errors import ImproperlyConfigured

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
    login_route = "obox_login"
    logout_route = "obox_logout"
    account_route = "obox_account"

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        *,
        backend: Backend | None = None,
        secure: bool = True,
        max_age: int = 86400,
        prefix: str = "/auth",
        login_template: str = "obox/login.html",
        account_template: str = "obox/account.html",
    ) -> None:
        if max_age <= 0:
            raise ValueError("max_age must be positive.")
        if backend is None:
            if session_factory is None:
                raise ImproperlyConfigured("Provide a backend or session_factory.")
            backend = ModelBackend(session_factory)
        self.backend = backend
        self.secure, self.max_age = secure, max_age
        self.prefix = prefix.rstrip("/")
        self.login_template, self.account_template = login_template, account_template
        self.login_label = (
            getattr(backend, "username_field", "username").replace("_", " ").capitalize()
        )
        self.cookie_name = "__Host-obox_session" if secure else "obox_session"
        self.login_cookie = "__Host-obox_login" if secure else "obox_login"

    def anonymous(self) -> Identity:
        return Principal()

    def get_token(self, request: Request) -> str:
        return request.cookies.get(self.cookie_name, "")

    async def principal(self, request: Request) -> Identity:
        return await self.backend.resolve(self.get_token(request), request=request)

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

    def login_url(self, request: Request) -> str:
        return str(request.url_for(self.login_route))

    def logout_url(self, request: Request) -> str:
        return str(request.url_for(self.logout_route))

    def account_url(self, request: Request) -> str:
        return str(request.url_for(self.account_route))

    def get_router(self, app: Obox) -> APIRouter:
        from obox_builder.contrib.auth.views import router

        return router(app, self)

    def install(self, app: Obox, *, router: APIRouter | None = None) -> None:
        if getattr(app.state, "obox_auth", None) is not None:
            raise ImproperlyConfigured("Auth is already installed.")
        routes = self.get_router(app) if router is None else router
        names = {getattr(route, "name", None) for route in routes.routes}
        if not {self.login_route, self.logout_route} <= names:
            raise ImproperlyConfigured(
                "Auth routes must include the configured login and logout names."
            )
        app.include_router(routes)
        app.state.obox_auth = self
