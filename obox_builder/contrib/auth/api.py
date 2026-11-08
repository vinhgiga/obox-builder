"""JSON session authentication and opt-in personal API token management."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import AwareDatetime, Field, StrictStr, field_serializer

from obox_builder.builder.api_schema import ApiRequest, ApiResponse
from obox_builder.builder.http import ApiRoute
from obox_builder.builder.query import parameters
from obox_builder.context import Context
from obox_builder.contrib.auth.backends import TokenBackend
from obox_builder.errors import (
    BuilderValidationError,
    ImproperlyConfigured,
    NotAuthenticated,
    PermissionDenied,
)

if TYPE_CHECKING:
    from obox_builder.application import Obox
    from obox_builder.contrib.auth import Auth


class AuthRoute(ApiRoute):
    cache_control = "private, no-store"


class Credentials(ApiRequest):
    username: StrictStr = Field(min_length=1, max_length=1024)
    password: StrictStr = Field(min_length=1, max_length=1024, repr=False)


class CSRFToken(ApiResponse):
    csrf_token: str


class CurrentUser(ApiResponse):
    id: str
    username: str
    is_active: bool
    is_staff: bool
    is_superuser: bool
    permissions: list[str]
    roles: list[str]
    api_scopes: list[str] | None


class NewToken(ApiRequest):
    name: StrictStr = Field(min_length=1, max_length=150)
    scopes: list[StrictStr] = Field(min_length=1)
    expires_at: AwareDatetime


class TokenRead(ApiResponse):
    id: UUID
    name: str
    scopes: list[str]
    expires_at: datetime
    created_at: datetime

    @field_serializer("expires_at", "created_at")
    def utc(self, value: datetime) -> datetime:
        return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


class TokenCreated(TokenRead):
    token: str = Field(repr=False)


class TokenList(ApiResponse):
    items: list[TokenRead]


def current_user(who: Any) -> CurrentUser:
    if not getattr(who, "is_authenticated", False) or not getattr(who, "is_active", True):
        raise NotAuthenticated("Authentication is required.")
    scopes = getattr(who, "api_scopes", None)
    permissions = set(getattr(who, "permissions", ()))
    if scopes is not None:
        permissions = {scope for scope in scopes if who.has_perm(scope)}
    return CurrentUser(
        id=str(who.id),
        username=who.username,
        is_active=who.is_active,
        is_staff=getattr(who, "is_staff", False),
        is_superuser=getattr(who, "is_superuser", False),
        permissions=sorted(permissions),
        roles=sorted(getattr(who, "roles", ())),
        api_scopes=sorted(scopes) if scopes is not None else None,
    )


def build_router(app: Obox, auth: Auth) -> APIRouter:
    router = APIRouter(
        prefix=app.settings.api_prefix + "/auth",
        route_class=AuthRoute,
        tags=["auth"],
        responses={
            status: {
                "content": {
                    "application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}
                }
            }
            for status in (400, 401, 403, 404, 405, 406, 409, 415, 422, 500)
        },
    )
    identity = Depends(auth.principal)

    def cookie_request(request: Request) -> None:
        if "authorization" in request.headers:
            raise PermissionDenied("Use a cookie session for this operation.")

    @router.get(
        "/csrf", response_model=CSRFToken, name="obox_api_auth_csrf", openapi_extra={"security": []}
    )
    async def csrf(request: Request, response: Response, who: Any = identity) -> CSRFToken:
        parameters(request.query_params.multi_items())
        cookies = dict(request.cookies)
        if not cookies.get(auth.login_cookie):
            cookies[auth.login_cookie] = secrets.token_urlsafe(32)
            auth.set_cookie(response, auth.login_cookie, cookies[auth.login_cookie], 3600)
        return CSRFToken(csrf_token=app.csrf.token(request, who, cookies=cookies))

    @router.post(
        "/login",
        response_model=CurrentUser,
        name="obox_api_auth_login",
        openapi_extra={"security": []},
    )
    async def login(
        request: Request, response: Response, data: Credentials, who: Any = identity
    ) -> CurrentUser:
        cookie_request(request)
        if not request.cookies.get(auth.login_cookie):
            raise PermissionDenied("Obtain a CSRF token before signing in.")
        app.csrf.validate(request, who)
        token = await auth.login(
            data.username, data.password, previous=auth.get_token(request), request=request
        )
        if token is None:
            raise NotAuthenticated("Invalid username or password.")
        backend = auth.backend
        if backend is None:
            raise ImproperlyConfigured("Install Auth before using its backend.")
        result = current_user(await backend.resolve(token, request=request))
        auth.set_cookie(response, auth.cookie_name, token, auth.max_age)
        auth.set_cookie(response, auth.login_cookie, "", 0)
        return result

    @router.post("/logout", status_code=204, name="obox_api_auth_logout")
    async def logout(request: Request, who: Any = identity) -> Response:
        cookie_request(request)
        current_user(who)
        app.csrf.validate(request, who)
        if await request.body():
            raise BuilderValidationError({"__all__": ["Request body must be empty."]})
        await auth.logout(auth.get_token(request), request=request)
        response = Response(status_code=204)
        auth.set_cookie(response, auth.cookie_name, "", 0)
        return response

    @router.get("/me", response_model=CurrentUser, name="obox_api_auth_me")
    async def me(request: Request, who: Any = identity) -> CurrentUser:
        parameters(request.query_params.multi_items())
        return current_user(who)

    if auth.api_tokens:
        backend = cast(TokenBackend, auth.backend)

        async def token_context(request: Request, who: Any = identity) -> AsyncIterator[Context]:
            async with app.uow(
                principal=who, source="api", request=request, trace_id=request.state.trace_id
            ) as ctx:
                yield ctx

        context = Depends(token_context, scope="function")

        def owner(request: Request, who: Any) -> Any:
            cookie_request(request)
            current_user(who)
            return who.id

        @router.post(
            "/tokens",
            status_code=201,
            response_model=TokenCreated,
            name="obox_api_auth_token_create",
        )
        async def create_token(
            request: Request, data: NewToken, ctx: Context = context
        ) -> TokenCreated:
            user_id = owner(request, ctx.principal)
            app.csrf.validate(request, ctx.principal)
            known = {
                registration.builder.permission_name(action): (registration.service, action)
                for registration in app._registrations.values()
                if registration.builder.api.enabled
                for action in (
                    *registration.builder.api.operations,
                    *registration.builder.permissions,
                )
            }
            if len(set(data.scopes)) != len(data.scopes) or any(
                scope not in known for scope in data.scopes
            ):
                raise BuilderValidationError(
                    {"scopes": ["Choose unique, exposed permission scopes."]}
                )
            for scope in data.scopes:
                service, action = known[scope]
                await service.authorize(action, ctx)
            row, token = await backend.create_api_token(
                user_id,
                name=data.name,
                scopes=data.scopes,
                expires_at=data.expires_at,
                request=request,
            )
            return TokenCreated(
                **TokenRead.model_validate(row).model_dump(by_alias=False), token=token
            )

        @router.get("/tokens", response_model=TokenList, name="obox_api_auth_token_list")
        async def list_tokens(request: Request, who: Any = identity) -> TokenList:
            parameters(request.query_params.multi_items())
            rows = await backend.list_api_tokens(owner(request, who))
            return TokenList(items=[TokenRead.model_validate(row) for row in rows])

        @router.delete("/tokens/{id}", status_code=204, name="obox_api_auth_token_revoke")
        async def revoke_token(request: Request, id: UUID, who: Any = identity) -> Response:
            user_id = owner(request, who)
            app.csrf.validate(request, who)
            if await request.body():
                raise BuilderValidationError({"__all__": ["Request body must be empty."]})
            await backend.revoke_api_token(user_id, id)
            return Response(status_code=204)

    return router
