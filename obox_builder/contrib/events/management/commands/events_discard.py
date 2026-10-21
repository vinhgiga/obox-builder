from __future__ import annotations

import uuid

from obox_builder.core.management.base import CommandError, command


@command(help="Discard a pending/dead delivery.", requires_checks=True)
async def handle(delivery_id: uuid.UUID) -> None:
    from obox_builder.contrib.events.services import discard
    from obox_builder.core.management.utils import echo
    from obox_builder.db.session import session_scope

    async with session_scope() as db:
        if not await discard(db, delivery_id):
            raise CommandError(
                "Delivery does not exist or cannot be discarded in its current state."
            )
    echo(f"Discarded {delivery_id}.")
