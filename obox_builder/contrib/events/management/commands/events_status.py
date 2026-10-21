from __future__ import annotations

import sqlalchemy as sa

from obox_builder.core.management.base import command


@command(help="Show event delivery counts by queue and state.", requires_checks=True)
async def handle() -> None:
    from obox_builder.contrib.events.models import Delivery
    from obox_builder.core.management.utils import echo
    from obox_builder.db.session import session_scope

    async with session_scope() as db:
        rows = await db.execute(
            sa.select(Delivery.queue, Delivery.status, sa.func.count())
            .group_by(Delivery.queue, Delivery.status)
            .order_by(Delivery.queue, Delivery.status)
        )
        for queue, status, count in rows:
            echo(f"{queue}: {status} = {count}")
