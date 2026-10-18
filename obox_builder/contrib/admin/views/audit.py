"""Global audit feed and change-set detail views (``/admin/history``)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.contrib.admin.views.common import page_context
from obox_builder.contrib.admin.views.history import (
    DiffMask,
    before_cursor,
    diff_mask,
    entry_view,
    model_timeline_available,
)
from obox_builder.contrib.auth.dependencies import permission_required
from obox_builder.contrib.auth.models import User
from obox_builder.contrib.history import HISTORY_VIEW_PERMISSION
from obox_builder.contrib.history.models import OPS, SOURCES
from obox_builder.contrib.history.service import (
    FeedFilters,
    change_set_entries,
    feed_count,
    feed_entries,
)
from obox_builder.db.session import get_db
from obox_builder.http.shortcuts import render

AuditReaders = Annotated[User, Depends(permission_required(HISTORY_VIEW_PERMISSION))]

FEED_PAGE_SIZE = 50
CHANGE_SET_LIMIT = 200
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
    )
    return render(request, "admin/history.html", context)
