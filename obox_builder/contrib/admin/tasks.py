"""Admin import orchestration; each batch owns its transaction and checkpoint."""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import Request
from sqlalchemy.orm import undefer

from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.imports import AdminImportResource
from obox_builder.contrib.auth.models import User
from obox_builder.contrib.auth.permissions import get_user_permissions
from obox_builder.contrib.history.context import use
from obox_builder.contrib.imports import services
from obox_builder.contrib.imports.models import ImportSession
from obox_builder.contrib.jobs import task
from obox_builder.db.session import session_scope
from obox_builder.exceptions import PermissionDenied
from obox_builder.utils.module_loading import import_string

STATES = {
    "inspect": {"uploaded"},
    "parse": {"parsing"},
    "validate": {"validating"},
    "commit": {"queued", "committing"},
}


@task(name="admin.import_spreadsheet", queue="imports", time_limit=3600)
async def import_spreadsheet(session_id: str, revision: int, phase: str, site_path: str) -> None:
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
                user.permissions = await get_user_permissions(db, user.id)
                site = import_string(site_path)
                site.autodiscover()
                admin = site.get_registry().get(session.model_label)
                if admin is None or not admin.imports_available():
                    raise PermissionDenied("Import is no longer enabled for this model.")
                request = Request(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": admin.import_path(),
                        "headers": [],
                        "query_string": b"",
                        "scheme": "http",
                        "server": ("localhost", 80),
                    }
                )
                request.state.user = user
                ctx = AdminContext(
                    request=request,
                    user=user,
                    db=db,
                    site=site,
                    admin=admin,
                    extra={"import_id": session.id},
                )
                resource = AdminImportResource(ctx)
                resource.check_configuration(session.mode)
                resource.check_permissions(session.mode)
                if not admin.has_view_permission(ctx):
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
