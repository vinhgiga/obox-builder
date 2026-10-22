from __future__ import annotations

from obox_builder.core.management.base import CommandError, command


@command(help="Retry a failed or revoked task, retaining its id and history.", requires_checks=True)
async def handle(task_id: str) -> None:
    from obox_builder.contrib.jobs import retry
    from obox_builder.core.management.utils import echo
    from obox_builder.db.session import session_scope

    try:
        async with session_scope() as db:
            if not await retry(db, task_id):
                raise CommandError("Task does not exist or is not FAILURE/REVOKED.")
    except ValueError as exc:
        raise CommandError(str(exc)) from exc
    echo(f"Requeued {task_id}.")
