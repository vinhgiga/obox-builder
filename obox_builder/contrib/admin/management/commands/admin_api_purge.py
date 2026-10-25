from __future__ import annotations

import sqlalchemy as sa
import typer

from obox_builder.core.management.base import command


@command(help="Purge one batch of expired API idempotency keys.", requires_checks=True)
async def handle(batch_size: int = typer.Option(1000, "--batch-size", min=1, max=10_000)) -> None:
    from obox_builder.contrib.admin.models import ApiIdempotencyKey
    from obox_builder.core.management.utils import echo
    from obox_builder.core.utils.time import utcnow
    from obox_builder.db.session import session_scope

    async with session_scope() as db:
        candidates = (
            sa.select(ApiIdempotencyKey.id)
            .where(ApiIdempotencyKey.expires_at < utcnow())
            .order_by(ApiIdempotencyKey.expires_at, ApiIdempotencyKey.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        removed = (
            await db.scalars(
                sa.delete(ApiIdempotencyKey)
                .where(ApiIdempotencyKey.id.in_(candidates))
                .returning(ApiIdempotencyKey.id)
            )
        ).all()
    echo(f"Purged {len(removed)} API idempotency keys.")
