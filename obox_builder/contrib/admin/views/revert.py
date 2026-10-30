"""Revert/restore (ADR §9): forward changes that never edit existing entries.

A revert re-applies ``old`` values through the standard form path
(``FieldType.from_input`` + the Pydantic schema), so validators and
constraints still run; the capture engine then writes a new entry with
``source='revert'`` and ``reverts_id`` pointing at the reverted entry.

Variants: reverting an ``update`` restores the previous values; reverting a
``delete`` re-inserts the row with its original PK when unused (recorded as
``create``); reverting a ``create`` deletes the row; reverting a whole change
set applies every entry in reverse ``id`` order inside one transaction and
fails entirely on any blocked/conflicting entry.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from fastapi import Request
from fastapi.responses import RedirectResponse

from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.rendering import render
from obox_builder.contrib.admin.services.history import diff_mask
from obox_builder.contrib.admin.views.common import ensure_csrf, flash, page_context
from obox_builder.contrib.history import HISTORY_RESTORE_PERMISSION
from obox_builder.contrib.history.models import RowChange
from obox_builder.contrib.history.revert import (
    OP_PERMISSION,
    BlockedField,
    Conflict,
    RevertBlocked,
    RevertConflict,
    RevertError,
    RevertPlan,
    _plan_for_change_set,
    blocked_fields,
    build_plan,
    plan_from_row,
    revert_plans,
)
from obox_builder.contrib.history.service import (
    to_entry,
)
from obox_builder.exceptions import PermissionDenied

logger = logging.getLogger("obox.history.audit")


def _forbidden(request: Request, reason: str) -> Any:
    return render(
        request,
        "admin/errors/403.html",
        {"site_name": "obox admin", "reason": reason},
        status_code=403,
    )


def _not_found(request: Request, message: str) -> Any:
    return render(
        request,
        "admin/errors/404.html",
        {"site_name": "obox admin", "message": message},
        status_code=404,
    )


async def _blocked_response(
    ctx: AdminContext,
    exc: RevertError,
    *,
    subtitle: str,
    back_url: str,
    force_url: str = "",
) -> Any:
    conflict = isinstance(exc, RevertConflict)
    context = await page_context(
        ctx.request,
        heading="Revert conflict" if conflict else "Revert blocked",
        subtitle=subtitle,
        back_url=back_url,
        blocked=[item for plan in exc.plans for item in plan.blocked],
        conflicts=[item for plan in exc.plans for item in plan.conflicts],
        force_url=force_url if conflict else "",
    )
    return render(ctx.request, "admin/revert.html", context, status_code=409)


async def revert_entry(
    ctx: AdminContext,
    *,
    pk: str,
    entry_id: int,
) -> Any:
    """``POST /admin/<path>/{pk}/history/{entry_id}/revert``."""
    admin = ctx.admin
    if admin is None:  # pragma: no cover - model routes always set it
        raise RuntimeError("AdminContext without a ModelView on a model route.")
    try:
        pk_value = admin.parse_pk(pk)
    except LookupError:
        return _not_found(ctx.request, f"Invalid id {pk!r}.")
    if not ctx.user.has_perm(HISTORY_RESTORE_PERMISSION):
        return _forbidden(ctx.request, "You do not have permission to restore changes.")
    try:
        visible = await ctx.db.get(admin.model, pk_value)
        if visible is not None:
            visible = await admin.get_object(ctx, pk)
        admin.check_permission(ctx, "view", visible)
        plan = await build_plan(
            ctx.operation_context(),
            admin.resource,
            pk_value,
            entry_id,
            mask=diff_mask(admin.model, user=ctx.user, admin=admin, ctx=ctx),
        )
    except LookupError as exc:
        return _not_found(ctx.request, str(exc))
    action = OP_PERMISSION.get(plan.entry.op, "change")
    if not (
        admin.has_add_permission(ctx)
        if action == "add"
        else getattr(admin, f"has_{action}_permission")(ctx, plan.target)
    ):
        return _forbidden(
            ctx.request,
            f"Reverting this change requires the '{admin.permission_name(action)}' permission.",
        )
    form = await ctx.request.form()
    token = form.get("csrf_token")
    await ensure_csrf(ctx.request, token if isinstance(token, str) else None)
    force = form.get("force") == "1"
    history_url = admin.history_path(pk_value)
    if plan.noop:
        await flash(ctx.request, "Nothing to revert: the row is already in that state.", "warning")
        return RedirectResponse(history_url, status_code=303)
    try:
        if plan.blocked:
            raise RevertBlocked("This entry cannot be restored through this view.", [plan])
        history_resource = ctx.site.resource_registry.for_model(RowChange)
        await history_resource.execute(
            "restore", ctx.operation_context(), {"force": force}, target=entry_id
        )
    except RevertError as exc:
        return await _blocked_response(
            ctx,
            exc,
            subtitle=f"{admin.name} {plan.entry.object_repr}",
            back_url=history_url,
            force_url=f"{history_url}/{entry_id}/revert",
        )
    logger.info(
        "Reverted entry #%d (%s#%s) by %s.",
        entry_id,
        admin.model_label,
        pk_value,
        getattr(ctx.user, "username", "system"),
    )
    await flash(ctx.request, f"Change #{entry_id} was reverted.")
    return RedirectResponse(history_url, status_code=303)


async def revert_change_set(
    ctx: AdminContext,
    change_set_id: UUID,
    *,
    allowed: frozenset[str] | None,
) -> Any:
    """``POST /admin/history/{change_set_id}/revert`` (reverse order, one flush)."""
    if not ctx.user.has_perm(HISTORY_RESTORE_PERMISSION):
        return _forbidden(ctx.request, "You do not have permission to restore changes.")
    rows = list(
        await ctx.db.scalars(
            sa.select(RowChange)
            .where(RowChange.change_set_id == change_set_id)
            .order_by(RowChange.id.desc())
        )
    )
    if not rows:
        return _not_found(ctx.request, f"Change set {change_set_id} does not exist.")
    plans: list[RevertPlan] = []
    for row in rows:
        entry = to_entry(row)
        plan = await _plan_for_change_set(
            ctx.operation_context(), ctx.site.resource_registry, entry, allowed=allowed
        )
        view = (
            ctx.site.get_model_admin_for(plan.resource.model) if plan.resource is not None else None
        )
        visible_label = "Restricted record"
        if view is not None:
            child = replace(ctx, admin=view, obj=plan.target)
            try:
                if plan.target is not None:
                    await view.get_object(child, str(plan.pk_value))
                view.check_permission(child, "view", plan.target)
                action = OP_PERMISSION.get(entry.op, "change")
                view.check_permission(child, action, plan.target)
                visible_label = entry.object_repr
                hidden = blocked_fields(
                    entry,
                    {spec.name: spec for spec in view.get_form_specs(child)},
                    mask=diff_mask(view.model, user=ctx.user, admin=view, ctx=child),
                )
                plan.blocked.extend(item for item in hidden if item not in plan.blocked)
            except (LookupError, PermissionDenied):
                plan.blocked.append(
                    BlockedField(
                        name="",
                        label="Restricted record",
                        reason="This record is unavailable through this view.",
                    )
                )
        plan.blocked = [
            replace(item, model_label=entry.model_label, object_repr=visible_label)
            for item in plan.blocked
        ]
        plan.conflicts = [
            replace(item, model_label=entry.model_label, object_repr=visible_label)
            for item in plan.conflicts
        ]
        plans.append(plan)
    form = await ctx.request.form()
    token = form.get("csrf_token")
    await ensure_csrf(ctx.request, token if isinstance(token, str) else None)
    force = form.get("force") == "1"
    back_url = f"/admin/history/{change_set_id}"
    try:
        if any(plan.blocked for plan in plans):
            raise RevertBlocked("This change set cannot be restored through this view.", plans)
        history_resource = ctx.site.resource_registry.for_model(RowChange)
        await history_resource.execute(
            "restoreChangeSet",
            ctx.operation_context(),
            {"force": force, "change_set_id": change_set_id},
        )
    except RevertError as exc:
        return await _blocked_response(
            ctx,
            exc,
            subtitle=f"change set {change_set_id}",
            back_url=back_url,
            force_url=f"{back_url}/revert",
        )
    logger.info(
        "Reverted change set %s (%d entries) by %s.",
        change_set_id,
        len(plans),
        getattr(ctx.user, "username", "system"),
    )
    await flash(ctx.request, f"Change set reverted ({len(plans)} entries).")
    return RedirectResponse(back_url, status_code=303)


__all__ = [
    "BlockedField",
    "Conflict",
    "RevertPlan",
    "RevertError",
    "RevertBlocked",
    "RevertConflict",
    "blocked_fields",
    "build_plan",
    "plan_from_row",
    "revert_plans",
    "OP_PERMISSION",
    "revert_entry",
    "revert_change_set",
]
