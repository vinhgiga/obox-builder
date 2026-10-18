"""Global audit feed, change-set detail, export and change-set revert."""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.views.common import page_context
from obox_builder.contrib.admin.views.history import (
    DiffMask,
    before_cursor,
    diff_mask,
    entry_view,
    model_timeline_available,
)
from obox_builder.contrib.admin.views.revert import revert_change_set
from obox_builder.contrib.auth.dependencies import permission_required
from obox_builder.contrib.auth.models import User
from obox_builder.contrib.history import (
    HISTORY_EXPORT_PERMISSION,
    HISTORY_RESTORE_PERMISSION,
    HISTORY_VIEW_PERMISSION,
)
from obox_builder.contrib.history.export import export_entries, guard_cell
from obox_builder.contrib.history.models import OPS, SOURCES
from obox_builder.contrib.history.service import (
    FeedFilters,
    change_set_entries,
    feed_count,
    feed_entries,
    to_entry,
)
from obox_builder.db.session import get_db
from obox_builder.http.shortcuts import render

logger = logging.getLogger("obox.history.audit")

AuditReaders = Annotated[User, Depends(permission_required(HISTORY_VIEW_PERMISSION))]
AuditExporters = Annotated[User, Depends(permission_required(HISTORY_EXPORT_PERMISSION))]
AuditRestorers = Annotated[User, Depends(permission_required(HISTORY_RESTORE_PERMISSION))]

FEED_PAGE_SIZE = 50
CHANGE_SET_LIMIT = 200
EXPORT_LIMIT = 10_000
EXPORT_COLUMNS = (
    "id",
    "change_set_id",
    "occurred_at",
    "op",
    "source",
    "actor",
    "model",
    "object_pk",
    "object_repr",
    "changes",
)
_ORPHAN_MASK = DiffMask(visible=frozenset(), known=frozenset(), is_superuser=True)


def _timeline_admins() -> dict[str, Any]:
    from obox_builder.contrib.admin.site import site

    return {
        label: admin
        for label, admin in site.get_registry().items()
        if model_timeline_available(admin.model)
    }


def _scope(user: User) -> tuple[frozenset[str] | None, dict[str, Any]]:
    """``(allowed labels, admins)``; ``None`` labels = superuser (no restriction)."""
    admins = _timeline_admins()
    if user.is_superuser:
        return None, admins
    allowed = frozenset(
        label for label, admin in admins.items() if user.has_perm(admin.permission_name("view"))
    )
    return allowed, admins


def _parse_day(raw: str) -> datetime | None:
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return None
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=UTC)


def _feed_filters(request: Request, allowed: frozenset[str] | None) -> FeedFilters:
    params = request.query_params
    model = params.get("model", "").strip()
    labels = allowed
    if model:
        labels = frozenset() if allowed is not None and model not in allowed else frozenset({model})
    source = params.get("source", "").strip()
    op = params.get("op", "").strip()
    since_raw = params.get("since", "").strip()
    until_raw = params.get("until", "").strip()
    until_day = _parse_day(until_raw) if until_raw else None
    return FeedFilters(
        model_labels=labels,
        actor=params.get("actor", "").strip()[:150],
        source=source if source in SOURCES else "",
        op=op if op in OPS else "",
        since=_parse_day(since_raw) if since_raw else None,
        until=until_day + timedelta(days=1) if until_day is not None else None,
    )


def _rows(entries: list[Any], admins: dict[str, Any], *, user: User) -> list[dict[str, Any]]:
    masks: dict[str, DiffMask] = {}
    rows: list[dict[str, Any]] = []
    for entry in entries:
        mask = masks.get(entry.model_label)
        if mask is None:
            admin = admins.get(entry.model_label)
            mask = (
                diff_mask(admin.model, admin=admin, user=user)
                if admin is not None
                else _ORPHAN_MASK
            )
            masks[entry.model_label] = mask
        view = entry_view(entry, mask=mask)
        admin = admins.get(entry.model_label)
        view["timeline_url"] = admin.history_path(entry.object_pk) if admin else ""
        view["model_name"] = admin.plural_name if admin else entry.model_label
        rows.append(view)
    return rows


async def history_feed(
    request: Request,
    user: AuditReaders,
    db: AsyncSession = Depends(get_db),
) -> Response:
    allowed, admins = _scope(user)
    filters = _feed_filters(request, allowed)
    before = before_cursor(request)
    entries = await feed_entries(db, filters=filters, before=before, limit=FEED_PAGE_SIZE)
    count = await feed_count(db, filters=filters)
    older = entries[-1].id if len(entries) == FEED_PAGE_SIZE else None
    filter_models = sorted(
        (
            {"label": label, "name": admin.plural_name}
            for label, admin in admins.items()
            if allowed is None or label in allowed
        ),
        key=lambda item: (item["name"], item["label"]),
    )
    context = await page_context(
        request,
        heading="Audit log",
        subtitle="Change feed",
        count=count,
        entries=_rows(entries, admins, user=user),
        older=older,
        before=before,
        filters={
            "model": request.query_params.get("model", "").strip(),
            "actor": filters.actor,
            "source": filters.source,
            "op": filters.op,
            "since": request.query_params.get("since", "").strip(),
            "until": request.query_params.get("until", "").strip(),
        },
        sources=SOURCES,
        ops=OPS,
        filter_models=filter_models,
        timeline_url=request.url.path,
    )
    return render(request, "admin/history_feed.html", context)


async def history_change_set(
    request: Request,
    change_set_id: UUID,
    user: AuditReaders,
    db: AsyncSession = Depends(get_db),
) -> Response:
    allowed, admins = _scope(user)
    entries = await change_set_entries(
        db, change_set_id, model_labels=allowed, limit=CHANGE_SET_LIMIT
    )
    if not entries:
        return render(request, "admin/errors/404.html", {}, status_code=404)
    context = await page_context(
        request,
        heading="Change set",
        subtitle=entries[0].actor_label,
        count=len(entries),
        entries=_rows(entries, admins, user=user),
        older=None,
        before=None,
        back_url="/admin/history",
        timeline_url=f"/admin/history/{change_set_id}",
        can_revert=user.is_superuser and user.has_perm(HISTORY_RESTORE_PERMISSION),
        revert_url=f"/admin/history/{change_set_id}/revert",
    )
    return render(request, "admin/history.html", context)


async def history_change_set_revert(
    request: Request,
    change_set_id: UUID,
    user: AuditRestorers,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """``POST /admin/history/{change_set_id}/revert`` (reverse order, one flush)."""
    from obox_builder.contrib.admin.site import site

    allowed, _admins = _scope(user)
    ctx = AdminContext(request=request, user=user, db=db, site=site, admin=None)
    return await revert_change_set(ctx, change_set_id, allowed=allowed)


# -- export -----------------------------------------------------------------


def _change_text(view: dict[str, Any]) -> str:
    parts: list[str] = []
    for diff in view["diffs"]:
        parts.append(f"{diff['label']}: {diff['old']} -> {diff['new']}")
    for relation in view["m2m"]:
        added = ", ".join(relation["added"]) or "-"
        removed = ", ".join(relation["removed"]) or "-"
        parts.append(f"{relation['label']}: +[{added}] -[{removed}]")
    for diff in view["masked"]:
        parts.append(f"{diff['label']}: {diff['old']} -> {diff['new']} (masked)")
    for label in view["redacted"]:
        parts.append(f"{label}: (secret)")
    for label in view["unknown_old"]:
        parts.append(f"{label}: (unknown old)")
    if view["truncated"]:
        parts.append("(payload truncated)")
    return "; ".join(parts)


async def _export_records(
    db: AsyncSession,
    filters: FeedFilters,
    admins: dict[str, Any],
    *,
    user: User,
) -> list[dict[str, str]]:
    """Masked feed rows in chronological order (permission-aware, ADR §8)."""
    masks: dict[str, DiffMask] = {}
    records: list[dict[str, str]] = []
    async for row in export_entries(db, filters, batch_size=EXPORT_LIMIT):
        mask = masks.get(row.model_label)
        if mask is None:
            admin = admins.get(row.model_label)
            mask = (
                diff_mask(admin.model, admin=admin, user=user)
                if admin is not None
                else _ORPHAN_MASK
            )
            masks[row.model_label] = mask
        view = entry_view(to_entry(row), mask=mask)
        records.append(
            {
                "id": str(view["id"]),
                "change_set_id": str(view["change_set_id"]),
                "occurred_at": view["occurred_at"].isoformat(),
                "op": view["op"],
                "source": view["source"],
                "actor": view["actor_label"],
                "model": view["model_label"],
                "object_pk": view["object_pk"],
                "object_repr": view["object_repr"],
                "changes": _change_text(view),
            }
        )
    return records


async def history_export(
    request: Request,
    user: AuditExporters,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """``GET /admin/history/export``: the filtered feed as CSV or NDJSON."""
    fmt = request.query_params.get("format", "csv").strip().lower() or "csv"
    if fmt not in ("csv", "ndjson"):
        return render(
            request,
            "admin/errors/400.html",
            {"site_name": "obox admin", "message": f"Unknown export format {fmt!r}."},
            status_code=400,
        )
    allowed, admins = _scope(user)
    filters = _feed_filters(request, allowed)
    records = await _export_records(db, filters, admins, user=user)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    if fmt == "ndjson":
        body = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
        media_type = "application/x-ndjson; charset=utf-8"
    else:
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(EXPORT_COLUMNS)
        for record in records:
            writer.writerow(guard_cell(record[column]) for column in EXPORT_COLUMNS)
        body = buffer.getvalue()
        media_type = "text/csv; charset=utf-8"
    logger.info(
        "Exported %d entries (format=%s) by %s.",
        len(records),
        fmt,
        getattr(user, "username", "system"),
    )
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="history-{stamp}.{fmt}"'},
    )
