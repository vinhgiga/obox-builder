from __future__ import annotations

from datetime import timedelta

import sqlalchemy as sa
import typer

from obox_builder.core.management.base import command


@command(help="Purge one batch of old terminal tasks and their attempts.", requires_checks=True)
async def handle(
    days: int = typer.Option(7, "--days", min=1),
    batch_size: int = typer.Option(1000, "--batch-size", min=1, max=10_000),
) -> None:
    from obox_builder.contrib.jobs.models import Task
    from obox_builder.core.management.utils import echo
    from obox_builder.core.utils.time import utcnow
    from obox_builder.db.session import session_scope

    async with session_scope() as db:
        candidates = (
            sa.select(Task.id)
            .where(
                Task.status.in_(("SUCCESS", "FAILURE", "REVOKED")),
                Task.date_done < utcnow() - timedelta(days=days),
            )
            .order_by(Task.date_done, Task.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        removed = (
            await db.scalars(sa.delete(Task).where(Task.id.in_(candidates)).returning(Task.id))
        ).all()
    echo(f"Purged {len(removed)} tasks and their attempts.")
