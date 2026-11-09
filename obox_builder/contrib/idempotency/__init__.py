"""Transactional, opt-in receipts for retrying creates and batch writes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from obox_builder.builder.query import invalid
from obox_builder.contrib.idempotency.models import Receipt
from obox_builder.errors import ImproperlyConfigured, NotAuthenticated, ResourceConflict

if TYPE_CHECKING:
    from obox_builder.application import Obox
    from obox_builder.context import Context


class Idempotency:
    name = "idempotency"
    requires: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        ttl: timedelta = timedelta(hours=24),
        partition: Callable[[Context], str] | None = None,
    ) -> None:
        if ttl <= timedelta(0):
            raise ImproperlyConfigured("Idempotency.ttl must be positive.")
        self.ttl, self.partition = ttl, partition

    def setup(self, app: Obox) -> None:
        if hasattr(self, "app"):
            raise ImproperlyConfigured("Idempotency is already installed.")
        self.app = app

    async def begin(self, payload: Any, ctx: Context) -> tuple[Receipt | None, bool]:
        request = ctx.request
        if request is None or "idempotency-key" not in request.headers:
            return None, False
        keys = request.headers.getlist("idempotency-key")
        key = keys[0]
        if len(keys) != 1 or not 1 <= len(key) <= 255 or any(not 33 <= ord(c) <= 126 for c in key):
            raise invalid("Idempotency-Key", "Use one key of 1-255 visible ASCII characters.")
        if not getattr(ctx.principal, "is_authenticated", False) or ctx.principal.id is None:
            raise NotAuthenticated("Idempotency requires an authenticated identity.")
        partition = self.partition(ctx) if self.partition else "default"
        if self.partition is None and ctx.tenant is not None:
            raise ImproperlyConfigured(
                "Tenant idempotency requires an explicit partition resolver."
            )
        if not isinstance(partition, str) or not partition or len(partition) > 255:
            raise ImproperlyConfigured(
                "Use a nonempty idempotency partition of at most 255 characters."
            )
        binding = [
            partition,
            str(ctx.tenant),
            str(ctx.principal.id),
            request.method,
            request.scope.get("root_path", "") + request.url.path,
            key,
        ]
        digest = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()
        now = datetime.now(UTC)
        dialect = ctx.session.get_bind().dialect.name
        insert = {"sqlite": sqlite_insert, "postgresql": pg_insert}.get(dialect)
        if insert is None:
            raise ImproperlyConfigured("Idempotency supports SQLite and PostgreSQL.")
        statement: Any = insert(Receipt).values(
            key=digest, fingerprint=fingerprint, expires_at=now + self.ttl, response=None
        )
        statement = statement.on_conflict_do_update(
            index_elements=[Receipt.key],
            set_={
                "fingerprint": fingerprint,
                "expires_at": now + self.ttl,
                "response": None,
            },
            where=Receipt.expires_at <= now,
        ).returning(Receipt.key)
        reserved = await ctx.session.scalar(statement)
        receipt = (
            await ctx.session.scalars(
                sa.select(Receipt)
                .where(Receipt.key == digest)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).one()
        if reserved is not None:
            return receipt, False
        if receipt.fingerprint != fingerprint:
            raise ResourceConflict("This idempotency key was already used with a different body.")
        if receipt.response is None:
            raise ResourceConflict("The original request has not completed.")
        return receipt, True

    async def prune(self, ctx: Context) -> None:
        """Hosts may run this in their existing maintenance job."""
        await ctx.session.execute(sa.delete(Receipt).where(Receipt.expires_at <= datetime.now(UTC)))
