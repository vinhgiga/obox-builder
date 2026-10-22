from __future__ import annotations

import sqlalchemy as sa

from obox_builder.core.management.base import command


@command(help="Show task counts by queue and state.", requires_checks=True)
async def handle() -> None:
    from obox_builder.contrib.jobs.models import Task
    from obox_builder.core.management.utils import echo
    from obox_builder.db.session import session_scope

    async with session_scope() as db:
        rows = await db.execute(
            sa.select(Task.queue, Task.status, sa.func.count())
            .group_by(Task.queue, Task.status)
            .order_by(Task.queue, Task.status)
        )
        for queue, status, count in rows:
            echo(f"{queue}: {status} = {count}")
