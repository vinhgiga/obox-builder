from __future__ import annotations

import uuid

from obox_builder.core.management.base import CommandError, command


@command(help="Requeue a terminal delivery, preserving its attempt history.", requires_checks=True)
async def handle(delivery_id: uuid.UUID) -> None:
    from obox_builder.contrib.events.services import redeliver
    from obox_builder.core.management.utils import echo
    from obox_builder.db.session import session_scope

    async with session_scope() as db:
        try:
            if not await redeliver(db, delivery_id):
                raise CommandError("Delivery does not exist or is not terminal.")
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
    echo(f"Requeued {delivery_id}.")
