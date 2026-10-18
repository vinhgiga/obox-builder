"""Timeline helpers: shape history entries for the admin templates."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.contrib.history.service import (
    RowChangeEntry,
    entry_count,
    history_entries,
    value_text,
)

TIMELINE_PAGE_SIZE = 20
CARD_LIMIT = 5


def model_timeline_available(model: type[Any]) -> bool:
    """True when the history app is installed and the model opts into tracking."""
    from obox_builder.apps import apps
    from obox_builder.contrib.admin.deps import HISTORY_APP
    from obox_builder.contrib.history import is_tracked

    return apps.is_installed(HISTORY_APP) and is_tracked(model)


def timeline_available(admin: Any) -> bool:
    """``model_timeline_available`` for a registered ``ModelAdmin``."""
    return model_timeline_available(admin.model)


def before_cursor(request: Request) -> int | None:
    """Keyset cursor: ``?before=<id>`` moves the window toward older entries."""
    raw = request.query_params.get("before")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def entry_view(entry: RowChangeEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "op": entry.op,
        "occurred_at": entry.occurred_at,
        "actor_label": entry.actor_label,
        "source": entry.source,
        "object_repr": entry.object_repr,
        "diffs": [
            {"label": diff.label, "old": value_text(diff.old), "new": value_text(diff.new)}
            for diff in entry.diffs
        ],
        "redacted": list(entry.redacted),
        "unknown_old": list(entry.unknown_old),
        "truncated": entry.truncated,
    }


async def build_card(db: AsyncSession, model: type[Any], pk: Any, *, url: str) -> dict[str, Any]:
    """Latest entries for the change-form card (never paginated)."""
    count = await entry_count(db, model, pk)
    entries = await history_entries(db, model, pk, limit=CARD_LIMIT)
    return {
        "count": count,
        "entries": [entry_view(entry) for entry in entries],
        "url": url,
    }


async def build_page(
    db: AsyncSession, model: type[Any], pk: Any, *, before: int | None
) -> dict[str, Any]:
    """One keyset page of the full timeline (newest first)."""
    count = await entry_count(db, model, pk)
    entries = await history_entries(db, model, pk, before=before, limit=TIMELINE_PAGE_SIZE)
    older = entries[-1].id if len(entries) == TIMELINE_PAGE_SIZE else None
    return {
        "count": count,
        "entries": [entry_view(entry) for entry in entries],
        "older": older,
        "before": before,
    }


async def timeline_context(
    db: AsyncSession,
    model: type[Any],
    pk: Any,
    *,
    before: int | None,
    heading: str,
    subtitle: str,
    back_url: str,
    timeline_url: str,
) -> dict[str, Any]:
    """Template context for the standalone timeline page (all entry points)."""
    return {
        "heading": heading,
        "subtitle": subtitle,
        "back_url": back_url,
        "timeline_url": timeline_url,
        **await build_page(db, model, pk, before=before),
    }
