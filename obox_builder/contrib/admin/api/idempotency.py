"""A uniqueness claim and its response commit atomically with record writes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from fastapi import Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.conf import settings
from obox_builder.contrib.admin.api.problems import Problem
from obox_builder.contrib.admin.models import ApiIdempotencyKey
from obox_builder.core.utils.time import ensure_aware, utcnow


async def execute(
    request: Request,
    db: AsyncSession,
    principal: str,
    body: dict[str, Any],
    operation: Callable[[], Awaitable[Response]],
    *,
    authorize_replay: Callable[[Any], Awaitable[None]] | None = None,
) -> Response:
    key = request.headers.get("idempotency-key")
    if key is None:
        return await operation()
    if not 1 <= len(key) <= 255 or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise Problem(
            400,
            "invalid_idempotency_key",
            "Idempotency-Key must contain 1–255 printable characters.",
        )
    fingerprint = hashlib.sha256(
        json.dumps(
            body, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    scope = sa.select(ApiIdempotencyKey).where(
        ApiIdempotencyKey.principal == principal, ApiIdempotencyKey.idempotency_key == key
    )
    now = utcnow()
    for _attempt in range(2):
        row = ApiIdempotencyKey(
            principal=principal,
            idempotency_key=key,
            method=request.method,
            path=request.url.path,
            fingerprint=fingerprint,
            response_status=0,
            response_body=None,
            response_headers={},
            created_at=now,
            expires_at=now + timedelta(seconds=settings.API_IDEMPOTENCY_TTL_SECONDS),
        )
        try:
            async with db.begin_nested():
                db.add(row)
                await db.flush()
        except IntegrityError:
            existing = await db.scalar(scope.with_for_update())
            if existing is None:
                continue
            if ensure_aware(existing.expires_at) <= now:
                await db.delete(existing)
                await db.flush()
                continue
            if (existing.method, existing.path, existing.fingerprint) != (
                request.method,
                request.url.path,
                fingerprint,
            ):
                raise Problem(
                    422, "idempotency_conflict", "This key was used for a different request."
                ) from None
            if authorize_replay is not None:
                await authorize_replay(existing.response_body)
            headers = existing.response_headers
            return (
                Response(status_code=existing.response_status, headers=headers)
                if existing.response_status == 204
                else JSONResponse(
                    existing.response_body, status_code=existing.response_status, headers=headers
                )
            )
        response = await operation()
        if len(response.body) > settings.API_MAX_BODY_BYTES:
            raise Problem(413, "payload_too_large", "Response is too large to replay safely.")
        row.response_status = response.status_code
        row.response_body = json.loads(bytes(response.body)) if response.body else None
        row.response_headers = {
            name: value
            for name, value in response.headers.items()
            if name.lower() in {"etag", "location", "content-type"}
        }
        await db.flush()
        return response
    raise Problem(409, "conflict", "The idempotency claim changed; retry the request.")
