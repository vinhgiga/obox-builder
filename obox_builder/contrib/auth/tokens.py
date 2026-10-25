"""Opaque personal access tokens; only digests reach the database."""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.conf import settings
from obox_builder.core.utils.time import ensure_aware, utcnow

if TYPE_CHECKING:
    from obox_builder.contrib.auth.models import ApiToken

SCOPE = re.compile(r"(?:\*|[a-z][a-z0-9_]*\.(?:\*|[a-z][a-z0-9_]*)):(?:read|write|delete|\*)")
TOKEN = re.compile(r"obx_pat_[a-f0-9]{8}_[A-Za-z0-9_-]{43}")


def scope_allows(scopes: Sequence[str], label: str, operation: str) -> bool:
    for scope in scopes:
        resource, _, action = scope.partition(":")
        if action in {operation, "*"} and (
            resource == "*" or resource == label or resource == label.partition(".")[0] + ".*"
        ):
            return True
    return False


async def create_token(
    db: AsyncSession,
    user_id: int,
    name: str,
    scopes: list[str],
    *,
    expires_at: datetime | None = None,
) -> tuple[str, ApiToken]:
    from obox_builder.contrib.auth.models import ApiToken

    now = utcnow()
    maximum = now + timedelta(days=settings.ADMIN_API_TOKEN_MAX_AGE_DAYS)
    expiry = ensure_aware(expires_at) if expires_at else maximum
    if (
        not 1 <= len(name.strip()) <= 100
        or not scopes
        or any(not SCOPE.fullmatch(scope) for scope in scopes)
    ):
        raise ValueError("A name and valid token scopes are required.")
    if not now < expiry <= maximum:
        raise ValueError("Token expiry must be within the configured maximum lifetime.")
    prefix = f"obx_pat_{secrets.token_hex(4)}"
    raw = f"{prefix}_{secrets.token_urlsafe(32)}"
    row = ApiToken(
        user_id=user_id,
        name=name.strip(),
        token_prefix=prefix,
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        scopes=sorted(set(scopes)),
        expires_at=expiry,
    )
    db.add(row)
    await db.flush()
    return raw, row


async def verify_token(db: AsyncSession, raw: str, *, for_update: bool = False) -> ApiToken | None:
    from obox_builder.contrib.auth.models import ApiToken

    if not TOKEN.fullmatch(raw):
        return None
    stmt = sa.select(ApiToken).where(
        ApiToken.token_hash == hashlib.sha256(raw.encode()).hexdigest(),
        ApiToken.revoked_at.is_(None),
        sa.or_(ApiToken.expires_at.is_(None), ApiToken.expires_at > utcnow()),
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return await db.scalar(stmt)
