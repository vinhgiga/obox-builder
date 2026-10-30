"""One committed transaction, session authentication and CSRF per API request."""

from __future__ import annotations

import json
import math
from typing import Annotated, Any

from fastapi import Depends, Request, Response, Security
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.conf import settings
from obox_builder.contrib.api.problems import Problem
from obox_builder.contrib.auth import csrf, services, sessions
from obox_builder.contrib.auth.models import User
from obox_builder.contrib.auth.permissions import get_actor
from obox_builder.contrib.auth.tokens import verify_token
from obox_builder.core.utils.time import ensure_aware, utcnow


async def api_db(request: Request) -> AsyncSession:
    return request.state.api_db


DB = Annotated[AsyncSession, Depends(api_db)]
_cookie = APIKeyCookie(name=settings.SESSION_COOKIE_NAME, auto_error=False)
_bearer = HTTPBearer(auto_error=False)


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin in settings.API_TRUSTED_ORIGINS and origin != "*":
        request.state.api_cors_origin = origin
        return
    if not csrf.check_origin(request):
        raise Problem(403, "csrf_failed", "Cross-origin request rejected.")


async def current_user(
    request: Request,
    response: Response,
    db: DB,
    token: str | None = Security(_cookie),
    bearer: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> User:
    if request.headers.get("authorization"):
        raw = bearer.credentials if bearer is not None else ""
        credential = await verify_token(db, raw)
        user = await db.get(User, credential.user_id) if credential is not None else None
        if credential is None or user is None or not user.is_active:
            raise Problem(401, "not_authenticated", "Invalid or expired access token.")
        request.state.api_token = credential
        request.state.auth_session = None
        request.state.api_principal = f"token:{credential.id}"
        request.state.user = user
        request.state.actor = await get_actor(
            db, user, catalog=request.state.api_registry.permissions
        )
        user.permissions = request.state.actor.permissions
        request.state.api_rate_headers = await request.state.api_limiter.check(
            request.state.api_principal, settings.API_RATE_LIMIT
        )
        if (
            credential.last_used_at is None
            or (utcnow() - ensure_aware(credential.last_used_at)).total_seconds() >= 60
        ):
            credential = await verify_token(db, raw, for_update=True)
            if credential is None:
                raise Problem(401, "not_authenticated", "Invalid or expired access token.")
            if (
                credential.last_used_at is None
                or (utcnow() - ensure_aware(credential.last_used_at)).total_seconds() >= 60
            ):
                credential.last_used_at = utcnow()
                credential.last_used_ip = request.client.host[:45] if request.client else None
        return user
    row = await sessions.get_session(db, token) if token else None
    backend = row.data.get("backend", settings.AUTHENTICATION_BACKENDS[0]) if row else None
    user = (
        await services.get_user_for_session(db, row.user_id, backend)
        if row and isinstance(backend, str)
        else None
    )
    if row is None or user is None or not user.is_active:
        raise Problem(401, "not_authenticated", "Authentication required.")
    if token and await sessions.touch_session(db, row):
        sessions.set_session_cookie(response, token)
        request.state.api_renew_token = token
    request.state.auth_session = row
    request.state.user = user
    request.state.actor = await get_actor(db, user, catalog=request.state.api_registry.permissions)
    user.permissions = request.state.actor.permissions
    if request.method not in csrf.SAFE_METHODS:
        check_origin(request)
        expected = csrf.get_csrf_token(row)
        import secrets

        provided = request.headers.get(csrf.CSRF_HEADER_NAME, "")
        if not secrets.compare_digest(provided, expected):
            raise Problem(403, "csrf_failed", "CSRF token is missing or invalid.")
    elif request.headers.get("origin") in settings.API_TRUSTED_ORIGINS:
        request.state.api_cors_origin = request.headers["origin"]
    request.state.api_principal = f"user:{user.id}"
    request.state.api_rate_headers = await request.state.api_limiter.check(
        request.state.api_principal, settings.API_RATE_LIMIT
    )
    return user


USER = Annotated[User, Depends(current_user)]


def session_only(request: Request) -> None:
    if getattr(request.state, "api_token", None) is not None:
        raise Problem(403, "permission_denied", "A session is required for this operation.")


def principal(request: Request) -> str:
    return f"{request.state.api_site.name}:{request.state.api_principal}"


async def json_body(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type not in {"application/json", "application/merge-patch+json"}:
        raise Problem(415, "unsupported_media_type", "A JSON body is required.")
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > settings.API_MAX_BODY_BYTES:
            raise Problem(413, "payload_too_large", "Request body is too large.")
    try:
        value = json.loads(
            content, parse_constant=lambda _: _invalid_json(), parse_float=_json_float
        )
    except (ValueError, RecursionError, UnicodeDecodeError) as exc:
        raise Problem(400, "invalid_json", "Invalid JSON body.") from exc
    if not isinstance(value, dict):
        raise Problem(422, "validation_error", "Expected a JSON object.")
    return value


def _invalid_json() -> Any:
    raise ValueError("Non-finite JSON number")


def _json_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    return value
