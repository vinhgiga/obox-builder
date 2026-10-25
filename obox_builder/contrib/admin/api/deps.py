"""One committed transaction, session authentication and CSRF per API request."""

from __future__ import annotations

import json
import math
from typing import Annotated, Any

from fastapi import Depends, Request, Response, Security
from fastapi.security import APIKeyCookie
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.conf import settings
from obox_builder.contrib.admin.api.problems import Problem
from obox_builder.contrib.auth import csrf, sessions
from obox_builder.contrib.auth.models import User
from obox_builder.contrib.auth.permissions import get_user_permissions


async def api_db(request: Request) -> AsyncSession:
    return request.state.api_db


DB = Annotated[AsyncSession, Depends(api_db)]
_cookie = APIKeyCookie(name=settings.SESSION_COOKIE_NAME, auto_error=False)


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin in settings.ADMIN_API_TRUSTED_ORIGINS and origin != "*":
        request.state.api_cors_origin = origin
        return
    if not csrf.check_origin(request):
        raise Problem(403, "csrf_failed", "Cross-origin request rejected.")


async def current_user(
    request: Request,
    response: Response,
    db: DB,
    token: str | None = Security(_cookie),
) -> User:
    if request.headers.get("authorization"):
        raise Problem(401, "not_authenticated", "Bearer authentication is not enabled.")
    row = await sessions.get_session(db, token) if token else None
    user = await db.get(User, row.user_id) if row else None
    if row is None or user is None or not user.is_active:
        raise Problem(401, "not_authenticated", "Authentication required.")
    if token and await sessions.touch_session(db, row):
        sessions.set_session_cookie(response, token)
        request.state.api_renew_token = token
    request.state.auth_session = row
    request.state.user = user
    user.permissions = await get_user_permissions(db, user.id)
    if request.method not in csrf.SAFE_METHODS:
        check_origin(request)
        expected = csrf.get_csrf_token(row)
        import secrets

        provided = request.headers.get(csrf.CSRF_HEADER_NAME, "")
        if not secrets.compare_digest(provided, expected):
            raise Problem(403, "csrf_failed", "CSRF token is missing or invalid.")
    elif request.headers.get("origin") in settings.ADMIN_API_TRUSTED_ORIGINS:
        request.state.api_cors_origin = request.headers["origin"]
    request.state.api_principal = f"user:{user.id}"
    request.state.api_rate_headers = await request.state.api_limiter.check(
        request.state.api_principal, settings.ADMIN_API_RATE_LIMIT
    )
    return user


USER = Annotated[User, Depends(current_user)]


async def json_body(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type not in {"application/json", "application/merge-patch+json"}:
        raise Problem(415, "unsupported_media_type", "A JSON body is required.")
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > settings.ADMIN_API_MAX_BODY_BYTES:
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
