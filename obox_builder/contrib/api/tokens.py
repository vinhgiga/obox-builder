"""Session-only self-service credentials; secrets are returned once."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from obox_builder.contrib.api.deps import DB, USER, session_only
from obox_builder.contrib.api.problems import Problem
from obox_builder.contrib.auth.models import ApiToken
from obox_builder.contrib.auth.tokens import SCOPE, create_token, scope_allows
from obox_builder.core.utils.time import utcnow


class TokenCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(min_length=1, max_length=100)
    expires_at: datetime | None = None


class TokenData(BaseModel):
    id: str
    name: str
    token_prefix: str
    scopes: list[str]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None


class TokenList(BaseModel):
    tokens: list[TokenData]


class TokenIssued(TokenData):
    token: str


def token_data(row: ApiToken) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "token_prefix": row.token_prefix,
        "scopes": row.scopes,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
    }


def register_tokens(router: APIRouter) -> None:
    @router.get("/tokens", response_model=TokenList)
    async def listing(request: Request, user: USER, db: DB) -> Any:
        session_only(request)
        rows = (
            await db.scalars(
                sa.select(ApiToken).where(ApiToken.user_id == user.id).order_by(ApiToken.id)
            )
        ).all()
        return {"tokens": [token_data(row) for row in rows]}

    @router.post("/tokens", status_code=201, response_model=TokenIssued)
    async def create(request: Request, data: TokenCreate, user: USER, db: DB) -> Any:
        session_only(request)
        labels = [resource.scope_name for resource in request.state.api_registry.resources.values()]
        if any(
            not SCOPE.fullmatch(scope)
            or not any(
                scope_allows([scope], label, action)
                for label in labels
                for action in ("read", "write", "delete")
            )
            for scope in data.scopes
        ):
            raise Problem(422, "validation_error", "Scopes must match registered resources.")
        try:
            raw, row = await create_token(
                db, user.id, data.name, data.scopes, expires_at=data.expires_at
            )
        except ValueError as exc:
            raise Problem(422, "validation_error", str(exc)) from exc
        return {**token_data(row), "token": raw}

    @router.delete("/tokens/{pk}", status_code=204)
    async def revoke(request: Request, pk: int, user: USER, db: DB) -> Response:
        session_only(request)
        row = await db.scalar(
            sa.select(ApiToken)
            .where(ApiToken.id == pk, ApiToken.user_id == user.id)
            .with_for_update()
        )
        if row is None:
            raise Problem(404, "not_found", "Token is unavailable.")
        if row.revoked_at is None:
            row.revoked_at = utcnow()
        return Response(status_code=204)
