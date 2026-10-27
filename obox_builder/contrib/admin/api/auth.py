"""JSON session login, logout, identity and password changes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from obox_builder.conf import settings
from obox_builder.contrib.admin.api.deps import DB, USER, check_origin, json_body, session_only
from obox_builder.contrib.admin.api.problems import Problem, problem_response
from obox_builder.contrib.auth import csrf, services, sessions
from obox_builder.contrib.auth.permissions import get_user_permissions
from obox_builder.contrib.auth.validators import PasswordValidationError


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=150, strict=True)
    password: str = Field(min_length=1, max_length=4096, strict=True)


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=4096, strict=True)
    new_password: str = Field(min_length=1, max_length=4096, strict=True)


class Identity(BaseModel):
    user: dict[str, Any]
    csrf_token: str
    permissions: list[str]
    scopes: list[str] = Field(default_factory=list)


def identity(user: Any, auth_session: Any) -> dict[str, Any]:
    return {
        "user": {
            "id": str(user.id),
            "username": user.username,
            "full_name": user.full_name,
            "is_superuser": user.is_superuser,
        },
        "csrf_token": csrf.get_csrf_token(auth_session),
        "permissions": sorted(user.permissions),
    }


def parse_body(schema: type[BaseModel], body: dict[str, Any]) -> Any:
    try:
        return schema.model_validate(body)
    except ValidationError as exc:
        errors: dict[str, list[str]] = {}
        for error in exc.errors():
            name = str(error["loc"][0]) if error["loc"] else "__all__"
            errors.setdefault(name, []).append(error["msg"])
        raise Problem(422, "validation_error", "Invalid request.", errors=errors) from exc


def register_auth(router: APIRouter) -> None:
    @router.post(
        "/auth/login",
        response_model=Identity,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": Login.model_json_schema()}},
            }
        },
    )
    async def login(request: Request, db: DB) -> Any:
        check_origin(request)
        peer = request.client.host if request.client else "unknown"
        request.state.api_rate_headers = await request.state.api_limiter.check(
            f"login:{peer}", settings.ADMIN_API_LOGIN_RATE_LIMIT
        )
        payload = parse_body(Login, await json_body(request))
        user = await services.authenticate(db, payload.username, payload.password)
        if user is None or not user.is_active:
            # A normal response lets the request transaction persist login lockout state.
            return problem_response(
                request, Problem(401, "not_authenticated", "Invalid username or password.")
            )
        old = request.cookies.get(settings.SESSION_COOKIE_NAME)
        if old:
            await sessions.delete_session_by_token(db, old)
        token, auth_session = await sessions.create_session(
            db, user.id, ip=peer, user_agent=request.headers.get("user-agent")
        )
        user.permissions = await get_user_permissions(db, user.id)
        request.state.user = user
        response = JSONResponse(identity(user, auth_session))
        sessions.set_session_cookie(response, token)
        return response

    @router.get("/auth/me", response_model=Identity)
    async def me(request: Request, user: USER) -> Any:
        if getattr(request.state, "api_token", None) is not None:
            token = request.state.api_token
            return {
                "user": {
                    "id": str(user.id),
                    "username": user.username,
                    "full_name": user.full_name,
                    "is_superuser": user.is_superuser,
                },
                "csrf_token": "",
                "permissions": [],
                "scopes": token.scopes,
            }
        return identity(user, request.state.auth_session)

    @router.post("/auth/logout", status_code=204)
    async def logout(request: Request, user: USER, db: DB) -> Response:
        session_only(request)
        await sessions.delete_session(db, request.state.auth_session)
        response = Response(status_code=204)
        sessions.clear_session_cookie(response)
        return response

    @router.post(
        "/auth/password",
        response_model=Identity,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": PasswordChange.model_json_schema()}},
            }
        },
    )
    async def password(request: Request, user: USER, db: DB) -> Any:
        session_only(request)
        payload = parse_body(PasswordChange, await json_body(request))
        try:
            await services.change_password(db, user, payload.current_password, payload.new_password)
        except PasswordValidationError as exc:
            raise Problem(
                422,
                "validation_error",
                "Password change failed.",
                errors={"new_password": exc.errors},
            ) from exc
        token, auth_session = await sessions.create_session(
            db,
            user.id,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        response = JSONResponse(identity(user, auth_session))
        sessions.set_session_cookie(response, token)
        return response
