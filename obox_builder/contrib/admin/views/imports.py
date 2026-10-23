"""Server-rendered upload, mapping, preview, progress and error reports."""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData, UploadFile

from obox_builder.conf import settings
from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.imports import AdminImportResource
from obox_builder.contrib.admin.options import ModelAdmin
from obox_builder.contrib.admin.rendering import render
from obox_builder.contrib.admin.site import AdminSite
from obox_builder.contrib.admin.tasks import import_spreadsheet
from obox_builder.contrib.admin.views.common import ensure_csrf, page_context
from obox_builder.contrib.auth.dependencies import login_required
from obox_builder.contrib.imports import services
from obox_builder.contrib.imports.models import ImportRow, ImportSession
from obox_builder.contrib.imports.parser import ImportFileError
from obox_builder.contrib.jobs.models import Task
from obox_builder.contrib.jobs.services import retry
from obox_builder.core.exceptions import ImproperlyConfigured, PermissionDenied
from obox_builder.db.session import get_db, session_scope
from obox_builder.utils.module_loading import import_string

BUSY = {"uploaded", "parsing", "validating", "queued", "committing"}


def register_import_routes(router: APIRouter, site: AdminSite, admin: ModelAdmin) -> None:
    async def context(
        request: Request, user: Any = Depends(login_required), db: AsyncSession = Depends(get_db)
    ) -> AdminContext:
        ctx = AdminContext(request=request, user=user, db=db, site=site, admin=admin)
        if not admin.imports_available() or not admin.has_view_permission(ctx):
            raise PermissionDenied("Import is unavailable.")
        return ctx

    async def start(ctx: AdminContext = Depends(context)) -> Any:
        if ctx.request.method == "GET":
            return await _page(ctx)
        try:
            async with _form(ctx.request) as data:
                await ensure_csrf(ctx.request, str(data.get("csrf_token", "")))
                upload = data.get("file")
                if not isinstance(upload, UploadFile):
                    raise ImportFileError("Choose a spreadsheet file.")
                content = await upload.read(settings.IMPORT_MAX_FILE_BYTES + 1)
                session = await services.create_import(
                    ctx.db,
                    AdminImportResource(ctx),
                    upload.filename or "",
                    content,
                    mode=str(data.get("mode", "insert")),
                )
                await _enqueue(ctx, session, "inspect")
            return RedirectResponse(f"{admin.import_path()}/{session.id}", status_code=303)
        except (ValueError, ImproperlyConfigured) as exc:
            return await _page(ctx, error=str(exc))

    async def detail(session_id: str, ctx: AdminContext = Depends(context)) -> Any:
        session = await _session(ctx, session_id)
        if session is None:
            return Response(status_code=404)
        if ctx.request.method == "GET":
            return await _page(ctx, session)
        try:
            async with ctx.request.form() as data:
                await ensure_csrf(ctx.request, str(data.get("csrf_token", "")))
                if str(session.revision) != str(data.get("revision", "")):
                    raise ValueError("This page is stale. Reload it before continuing.")
                action = data.get("action")
                resource = AdminImportResource(ctx)
                if action == "cancel" and session.status not in {"completed", "cancelled"}:
                    services.cancel_import(session, resource)
                elif action == "retry" and session.status in BUSY and session.task_id:
                    resource.check_permissions(session.mode)
                    if not await retry(ctx.db, session.task_id):
                        raise ValueError("This import cannot be retried yet.")
                elif action == "parse" and session.status == "setup":
                    resource.check_permissions(session.mode)
                    sheet = str(data.get("sheet", ""))
                    header = int(str(data.get("header_row", "1")))
                    if sheet not in session.sheets or not 1 <= header <= 100:
                        raise ValueError(
                            "Choose a visible sheet and a header row between 1 and 100."
                        )
                    session.sheet, session.header_row, session.status = sheet, header, "parsing"
                    session.revision += 1
                    await _enqueue(ctx, session, "parse")
                elif action == "map":
                    mapping = {
                        str(i): str(data[f"column:{i}"])
                        for i in range(len(session.headers))
                        if data.get(f"column:{i}")
                    }
                    await services.set_mapping(ctx.db, session, resource, mapping)
                    await _enqueue(ctx, session, "validate")
                elif action == "commit" and session.status == "review":
                    services.confirm_import(session, resource)
                    await _enqueue(ctx, session, "commit")
                else:
                    raise ValueError("This action is unavailable in the current state.")
            return RedirectResponse(f"{admin.import_path()}/{session.id}", status_code=303)
        except (ValueError, ImproperlyConfigured) as exc:
            return await _page(ctx, session, error=str(exc))

    async def report(session_id: str, ctx: AdminContext = Depends(context)) -> Any:
        session = await _session(ctx, session_id)
        if session is None:
            return Response(status_code=404)
        if session.status not in {"review", "completed", "failed", "cancelled"}:
            return Response(status_code=409)
        resource = AdminImportResource(ctx)
        visible = {str(i) for i, name in session.mapping.items() if name in resource.columns}
        headers = [header if str(i) in visible else "" for i, header in enumerate(session.headers)]

        async def records() -> AsyncIterator[str]:
            buffer = io.StringIO()
            writer = csv.writer(buffer)

            def line(values: list[Any]) -> str:
                buffer.seek(0)
                buffer.truncate()
                writer.writerow([_safe_cell(value) for value in values])
                return buffer.getvalue()

            yield "\ufeff" + line(["Source row", *headers, "Errors"])
            async with session_scope() as db:
                last = 0
                while True:
                    rows = list(
                        await db.scalars(
                            sa.select(ImportRow)
                            .where(
                                ImportRow.session_id == session_id,
                                ImportRow.action == "error",
                                ImportRow.id > last,
                            )
                            .order_by(ImportRow.id)
                            .limit(250)
                        )
                    )
                    if not rows:
                        break
                    for row in rows:
                        values = [
                            value if str(i) in visible else "" for i, value in enumerate(row.data)
                        ]
                        errors = "; ".join(
                            f"{field}: {', '.join(messages)}"
                            for field, messages in row.errors.items()
                            if field == "__all__" or field in resource.columns
                        )
                        yield line([row.number, *values, errors])
                        last = row.id

        return StreamingResponse(
            records(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="import-errors.csv"',
                "Cache-Control": "no-store",
            },
        )

    base, name = f"/{admin.path_prefix}/import", admin.route_prefix
    router.add_api_route(base, start, methods=["GET", "POST"], name=f"{name}_import")
    router.add_api_route(
        f"{base}/{{session_id}}/errors", report, methods=["GET"], name=f"{name}_import_errors"
    )
    router.add_api_route(
        f"{base}/{{session_id}}", detail, methods=["GET", "POST"], name=f"{name}_import_detail"
    )


async def _session(ctx: AdminContext, session_id: str) -> ImportSession | None:
    if ctx.admin is None:
        raise ValueError("A model admin is required.")
    return await ctx.db.scalar(
        sa.select(ImportSession)
        .where(
            ImportSession.id == session_id,
            ImportSession.owner_id == ctx.user.id,
            ImportSession.model_label == ctx.admin.model_label,
        )
        .with_for_update()
    )


async def _enqueue(ctx: AdminContext, session: ImportSession, phase: str) -> None:
    path = ctx.site.import_path
    if not path or import_string(path) is not ctx.site:
        raise ImproperlyConfigured(
            "Configure AdminSite(import_path=...) with its importable instance path."
        )
    task = await import_spreadsheet.apply_async(
        ctx.db,
        args=(session.id, session.revision, phase, path),
        dedup_key=f"import:{session.id}:{session.revision}:{phase}",
    )
    session.task_id = task.task_id


@asynccontextmanager
async def _form(request: Request) -> AsyncIterator[FormData]:
    original = request._receive
    received = 0

    async def receive() -> Any:
        nonlocal received
        message = await original()
        received += len(message.get("body", b""))
        if received > settings.IMPORT_MAX_FILE_BYTES + 1024 * 1024:
            raise ImportFileError("The upload exceeds the size limit.")
        return message

    request._receive = receive
    try:
        async with request.form(max_files=1, max_fields=5) as data:
            yield data
    finally:
        request._receive = original


async def _page(
    ctx: AdminContext, session: ImportSession | None = None, *, error: str = ""
) -> HTMLResponse:
    if ctx.admin is None:
        raise ValueError("A model admin is required.")
    resource = AdminImportResource(ctx)
    modes = []
    for mode in services.MODES:
        try:
            resource.check_configuration(mode)
            resource.check_permissions(mode)
        except (ImproperlyConfigured, PermissionDenied):
            continue
        modes.append(mode)
    rows: list[ImportRow] = []
    if session is not None and session.status in {
        "mapping",
        "review",
        "completed",
        "failed",
        "cancelled",
    }:
        page = max(
            1,
            min(
                100_000,
                int(ctx.request.query_params.get("page", "1"))
                if ctx.request.query_params.get("page", "1").isdigit()
                else 1,
            ),
        )
        statement = (
            sa.select(ImportRow)
            .where(ImportRow.session_id == session.id)
            .order_by(ImportRow.number)
        )
        if ctx.request.query_params.get("errors") == "1":
            statement = statement.where(ImportRow.action == "error")
        rows = list(await ctx.db.scalars(statement.offset((page - 1) * 50).limit(51)))
    else:
        page = 1
    mapping = (
        (session.mapping or services.suggest_mapping(session.headers, resource)) if session else {}
    )
    task_failed = False
    if session is not None and session.status in BUSY and session.task_id:
        task_failed = bool(
            await ctx.db.scalar(
                sa.select(Task.task_id).where(
                    Task.task_id == session.task_id, Task.status.in_(("FAILURE", "REVOKED"))
                )
            )
        )
    context = await page_context(
        ctx.request,
        admin=ctx.admin,
        session=session,
        error=error,
        modes=modes,
        specs=resource.specs,
        mapping=mapping,
        rows=rows[:50],
        page=page,
        has_next=len(rows) > 50,
        busy=session is not None and session.status in BUSY and not task_failed,
        task_failed=task_failed,
        counts=await services.summary(ctx.db, session) if session else {},
        max_rows=settings.IMPORT_MAX_ROWS,
        max_mb=settings.IMPORT_MAX_FILE_BYTES // (1024 * 1024),
    )
    response = render(ctx.request, "admin/imports/wizard.html", context)
    response.headers["Cache-Control"] = "no-store"
    return response


def _safe_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    return (
        "'" + text
        if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n"))
        else text
    )
