from __future__ import annotations

from obox_builder.core.management.base import CommandError, command


@command(
    help="Revoke queued or running work; external effects cannot be undone.", requires_checks=True
)
async def handle(task_id: str) -> None:
    from obox_builder.contrib.jobs import revoke
    from obox_builder.core.management.utils import echo
    from obox_builder.db.session import session_scope

    async with session_scope() as db:
        if not await revoke(db, task_id):
            raise CommandError("Task does not exist or has already finished.")
    echo(f"Revoked {task_id}.")
