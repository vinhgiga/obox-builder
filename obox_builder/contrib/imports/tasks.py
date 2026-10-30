"""Admin import orchestration; each batch owns its transaction and checkpoint."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import undefer

from obox_builder.contrib.auth.models import User
from obox_builder.contrib.auth.permissions import get_actor
from obox_builder.contrib.history.context import use
from obox_builder.contrib.imports import services
from obox_builder.contrib.imports.models import ImportSession
from obox_builder.contrib.imports.resources import ImportResource
from obox_builder.contrib.jobs import task
from obox_builder.db.session import session_scope
from obox_builder.exceptions import PermissionDenied
from obox_builder.resources import ModelResource, OperationContext, get_registry

STATES = {
    "inspect": {"uploaded"},
    "parse": {"parsing"},
    "validate": {"validating"},
    "commit": {"queued", "committing"},
}


@task(name="admin.import_spreadsheet", queue="imports", time_limit=3600)
async def import_spreadsheet(session_id: str, revision: int, phase: str, source: str = "") -> None:
    if phase not in STATES:
        raise ValueError("Unknown import phase.")
    try:
        more = True
        while more:
            async with session_scope() as db:
                if db.get_bind().dialect.name == "sqlite":
                    await db.connection(execution_options={"sqlite_begin_immediate": True})
                session = await db.scalar(
                    sa.select(ImportSession)
                    .where(ImportSession.id == session_id)
                    .options(undefer(ImportSession.source))
                    .with_for_update()
                )
                if (
                    session is None
                    or session.revision != revision
                    or session.status not in STATES[phase]
                ):
                    return
                user = await db.get(User, session.owner_id)
                if user is None or not user.is_active:
                    raise PermissionDenied("The importing user is inactive or no longer exists.")
                catalog = get_registry()
                definition = next(
                    (
                        item
                        for item in catalog.resources.values()
                        if item.scope_name == session.model_label
                    ),
                    None,
                )
                if not isinstance(definition, ModelResource):
                    raise PermissionDenied("The imported resource is no longer registered.")
                ctx = OperationContext(
                    await get_actor(db, user),
                    db=db,
                    registry=catalog,
                    metadata={"source": "job", "import_id": session.id},
                )
                resource = ImportResource(definition, ctx)
                resource.check_configuration(session.mode)
                resource.check_permissions(session.mode)
                if not definition.can_execute(ctx, "list"):
                    raise PermissionDenied("Viewing this model is no longer permitted.")
                async with use(
                    source="job",
                    actor_id=user.id,
                    actor_label=user.username,
                    reason=f"Spreadsheet import {session.id}",
                ):
                    more = False
                    if phase == "inspect":
                        services.inspect_file(session)
                    elif phase == "parse":
                        await services.parse_file(db, session)
                    elif phase == "validate":
                        more = await services.validate_batch(db, session, resource)
                    else:
                        more = await services.commit_batch(db, session, resource)
    except (ValueError, PermissionDenied) as exc:
        await _fail(session_id, revision, str(exc))
    except Exception:
        # Transient failures retain the checkpoint for the durable task retry.
        raise


async def _fail(session_id: str, revision: int, message: str) -> None:
    async with session_scope() as db:
        await db.execute(
            sa.update(ImportSession)
            .where(
                ImportSession.id == session_id,
                ImportSession.revision == revision,
                ImportSession.status.not_in(("completed", "cancelled")),
            )
            .values(status="failed", error=message[:500], source=None)
        )
