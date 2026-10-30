"""Shared history visibility and timeline data for admin adapters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.contrib.history.access import DiffMask
from obox_builder.contrib.history.service import (
    RowChangeEntry,
    entry_count,
    field_labels,
    history_entries,
    value_text,
)
from obox_builder.fields import FieldSpec

TIMELINE_PAGE_SIZE = 20
CARD_LIMIT = 5


@dataclass(frozen=True)
class RevertLinks:
    """Per-entry revert wiring for one timeline (``can_revert`` = permissions)."""

    base_url: str
    specs: dict[str, FieldSpec]
    can_revert: Callable[[RowChangeEntry], bool]

    def url(self, entry: RowChangeEntry) -> str:
        return f"{self.base_url}/{entry.id}/revert"


def diff_mask(model: type[Any], *, user: Any, admin: Any = None, ctx: Any = None) -> DiffMask:
    from obox_builder.contrib.history.access import diff_mask as resource_mask
    from obox_builder.resources import OperationContext, get_registry
    from obox_builder.security import Actor

    catalog = get_registry()
    context = (
        ctx.operation_context()
        if ctx is not None
        else OperationContext(
            Actor(
                id=getattr(user, "id", None),
                permissions=frozenset(name for name in catalog.permissions if user.has_perm(name)),
                is_superuser=bool(getattr(user, "is_superuser", False)),
            ),
            registry=catalog,
        )
    )
    mask = resource_mask(model, context)
    if admin is not None:
        displayed = {
            spec.name
            for spec in (*admin.get_form_specs(ctx), *admin.get_list_specs(ctx))
            if not spec.secret
        }
        from obox_builder.contrib.history.tracker import get_tracker

        tracker = get_tracker(model)
        if tracker is not None:
            displayed.update(item.name for item in tracker.relations)
        mask = replace(mask, visible=mask.visible & displayed)
    return mask


def model_timeline_available(model: type[Any]) -> bool:
    """True when the history app is installed and the model opts into tracking."""
    from obox_builder.apps import apps
    from obox_builder.contrib.admin.deps import HISTORY_APP
    from obox_builder.contrib.history import is_tracked

    return apps.is_installed(HISTORY_APP) and is_tracked(model)


def timeline_available(admin: Any) -> bool:
    """``model_timeline_available`` for a registered ``ModelView``."""
    return model_timeline_available(admin.model)


def before_cursor(request: Request) -> int | None:
    """Keyset cursor: ``?before=<id>`` moves the window toward older entries."""
    raw = request.query_params.get("before")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def entry_view(
    entry: RowChangeEntry,
    *,
    mask: DiffMask | None = None,
    revert: RevertLinks | None = None,
) -> dict[str, Any]:
    labels = field_labels(entry.model_label)
    if mask is not None:
        entry = replace(
            entry,
            diffs=tuple(diff for diff in entry.diffs if mask.allows(diff.name)),
            m2m=tuple(rel for rel in entry.m2m if mask.allows(rel.name)),
            masked=tuple(diff for diff in entry.masked if mask.allows(diff.name)),
            redacted=tuple(name for name in entry.redacted if mask.allows(name)),
            unknown_old=tuple(name for name in entry.unknown_old if mask.allows(name)),
        )
    view: dict[str, Any] = {
        "id": entry.id,
        "change_set_id": entry.change_set_id,
        "op": entry.op,
        "occurred_at": entry.occurred_at,
        "actor_label": entry.actor_label,
        "source": entry.source,
        "model_label": entry.model_label,
        "object_pk": entry.object_pk,
        "object_repr": entry.object_repr,
        "diffs": [
            {"label": diff.label, "old": value_text(diff.old), "new": value_text(diff.new)}
            for diff in entry.diffs
        ],
        "m2m": [
            {"label": diff.label, "added": list(diff.added), "removed": list(diff.removed)}
            for diff in entry.m2m
        ],
        "masked": [
            {"label": diff.label, "old": value_text(diff.old), "new": value_text(diff.new)}
            for diff in entry.masked
        ],
        "redacted": [labels.get(name, name) for name in entry.redacted],
        "unknown_old": [labels.get(name, name) for name in entry.unknown_old],
        "truncated": entry.truncated,
        "revertable": False,
        "revert_url": "",
    }
    if revert is not None:
        from obox_builder.contrib.history.revert import blocked_fields

        view["revertable"] = revert.can_revert(entry) and not blocked_fields(
            entry, revert.specs, mask=mask
        )
        view["revert_url"] = revert.url(entry)
    return view


async def build_card(
    db: AsyncSession,
    model: type[Any],
    pk: Any,
    *,
    url: str,
    mask: DiffMask | None = None,
    revert: RevertLinks | None = None,
) -> dict[str, Any]:
    """Latest entries for the change-form card (never paginated)."""
    count = await entry_count(db, model, pk)
    entries = await history_entries(db, model, pk, limit=CARD_LIMIT)
    return {
        "count": count,
        "entries": [entry_view(entry, mask=mask, revert=revert) for entry in entries],
        "url": url,
    }


async def build_page(
    db: AsyncSession,
    model: type[Any],
    pk: Any,
    *,
    before: int | None,
    mask: DiffMask | None = None,
    revert: RevertLinks | None = None,
) -> dict[str, Any]:
    """One keyset page of the full timeline (newest first)."""
    count = await entry_count(db, model, pk)
    entries = await history_entries(db, model, pk, before=before, limit=TIMELINE_PAGE_SIZE)
    older = entries[-1].id if len(entries) == TIMELINE_PAGE_SIZE else None
    return {
        "count": count,
        "entries": [entry_view(entry, mask=mask, revert=revert) for entry in entries],
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
    mask: DiffMask | None = None,
    revert: RevertLinks | None = None,
    deleted: bool = False,
) -> dict[str, Any]:
    """Template context for the standalone timeline page (all entry points)."""
    return {
        "heading": heading,
        "subtitle": subtitle,
        "back_url": back_url,
        "timeline_url": timeline_url,
        "deleted_target": deleted,
        **await build_page(db, model, pk, before=before, mask=mask, revert=revert),
    }
