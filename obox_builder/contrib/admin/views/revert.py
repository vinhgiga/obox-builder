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
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa
from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.options import ModelAdmin
from obox_builder.contrib.admin.rendering import render
from obox_builder.contrib.admin.views.common import ensure_csrf, flash, page_context
from obox_builder.contrib.history import HISTORY_RESTORE_PERMISSION
from obox_builder.contrib.history.context import CTX_KEY, from_request
from obox_builder.contrib.history.models import RowChange
from obox_builder.contrib.history.service import (
    RowChangeEntry,
    field_labels,
    model_for_label,
    to_entry,
)
from obox_builder.fields import FieldSpec
from obox_builder.forms import BoundForm, build_schema

if TYPE_CHECKING:
    from obox_builder.contrib.admin.views.history import DiffMask

logger = logging.getLogger("obox.history.audit")

#: Which model permission each variant needs on top of ``restore_row_change``.
OP_PERMISSION = {"update": "change", "delete": "add", "create": "delete"}


@dataclass
class BlockedField:
    """One field/relation (or whole entry) that cannot be restored."""

    name: str
    label: str
    reason: str
    model_label: str = ""
    object_repr: str = ""


@dataclass
class Conflict:
    """The current value differs from the recorded ``new`` (update revert)."""

    name: str
    label: str
    old: Any
    new: Any
    current: Any
    model_label: str = ""
    object_repr: str = ""


@dataclass
class RevertPlan:
    """What reverting one entry would do (or why it cannot)."""

    admin: ModelAdmin | None
    entry: RowChangeEntry
    pk_value: Any
    target: Any | None = None
    values: dict[str, Any] = field(default_factory=dict)
    blocked: list[BlockedField] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    #: True when the desired state already holds (nothing to do).
    noop: bool = False


class RevertError(Exception):
    """Base for refusal; carries the plans so the view can explain itself."""

    def __init__(self, message: str, plans: list[RevertPlan]) -> None:
        super().__init__(message)
        self.message = message
        self.plans = plans


class RevertBlocked(RevertError):
    """The entry (or change set) cannot be reverted at all."""


class RevertConflict(RevertError):
    """The row changed since the entry was recorded (``force`` can override)."""


# -- planning ---------------------------------------------------------------


def blocked_fields(
    entry: RowChangeEntry,
    specs: dict[str, FieldSpec],
    *,
    mask: DiffMask | None = None,
) -> list[BlockedField]:
    """Fields/relations that cannot be restored, with the reason."""
    labels = field_labels(entry.model_label)
    blocked: list[BlockedField] = []

    if entry.op == "create":
        return blocked

    for relation in entry.m2m:
        blocked.append(
            BlockedField(
                name=relation.name,
                label=relation.label,
                reason="relation changes cannot be restored.",
            )
        )
    for name in entry.redacted:
        blocked.append(
            BlockedField(name=name, label=labels.get(name, name), reason="secret value.")
        )
    for diff in entry.masked:
        blocked.append(BlockedField(name=diff.name, label=diff.label, reason="masked value."))
    for name in entry.unknown_old:
        blocked.append(
            BlockedField(name=name, label=labels.get(name, name), reason="old value unknown.")
        )
    for name in entry.truncated_names:
        blocked.append(
            BlockedField(name=name, label=labels.get(name, name), reason="value was truncated.")
        )
    for diff in entry.diffs:
        if mask is not None and not mask.allows(diff.name):
            blocked.append(
                BlockedField(name=diff.name, label=diff.label, reason="not visible to you.")
            )
            continue
        if diff.name not in specs:
            blocked.append(
                BlockedField(name=diff.name, label=diff.label, reason="not in the edit form.")
            )
    return blocked


async def build_plan(
    ctx: AdminContext,
    admin: ModelAdmin,
    pk_value: Any,
    entry_id: int,
    *,
    mask: DiffMask | None = None,
) -> RevertPlan:
    """Load one entry and work out how to revert it (``LookupError`` = 404)."""
    row = await ctx.db.get(RowChange, entry_id)
    if row is None or row.model_label != admin.model_label or row.object_pk != str(pk_value):
        raise LookupError(f"History entry {entry_id} does not belong to this row.")
    return await plan_from_row(ctx, admin, pk_value, to_entry(row), mask=mask)


async def plan_from_row(
    ctx: AdminContext,
    admin: ModelAdmin,
    pk_value: Any,
    entry: RowChangeEntry,
    *,
    mask: DiffMask | None = None,
) -> RevertPlan:
    specs = {spec.name: spec for spec in admin.get_form_specs()}
    plan = RevertPlan(admin=admin, entry=entry, pk_value=pk_value)
    plan.target = await ctx.db.get(admin.model, pk_value)
    if entry.op == "create":
        # Reverting a create is a confirmed delete; no values are restored.
        if plan.target is None:
            plan.noop = True
        return plan
    plan.blocked = blocked_fields(entry, specs, mask=mask)
    if entry.op == "delete":
        if plan.target is not None:
            plan.blocked.append(
                BlockedField(
                    name="",
                    label=f"#{entry.object_pk}",
                    reason="the primary key is in use by an existing row.",
                )
            )
            return plan
        plan.values = {diff.name: diff.old for diff in entry.diffs}
        return plan
    # update
    if plan.target is None:
        plan.blocked.append(
            BlockedField(
                name="",
                label=f"#{entry.object_pk}",
                reason="the row has been deleted since this change.",
            )
        )
        return plan
    plan.values = {diff.name: diff.old for diff in entry.diffs}
    blocked_names = {item.name for item in plan.blocked}
    for diff in entry.diffs:
        spec = specs.get(diff.name)
        if spec is None or diff.name in blocked_names:
            continue
        current = spec.type.diff_repr(getattr(plan.target, diff.name, None))
        if current != diff.new:
            plan.conflicts.append(
                Conflict(
                    name=diff.name,
                    label=diff.label,
                    old=diff.old,
                    new=diff.new,
                    current=current,
                )
            )
    return plan


# -- applying ---------------------------------------------------------------


def _bind_values(plan: RevertPlan, ctx: AdminContext, values: dict[str, Any]) -> BoundForm:
    admin = _admin_of(plan)
    specs = [spec for spec in admin.get_form_specs() if spec.name in values]
    schema = build_schema(
        specs,
        name=f"revert_{admin.app_label}_{admin.model_name}",
        mode="revert",
    )
    ctx.obj = plan.target
    return BoundForm.bind(specs, schema, values, base=ctx.record_context(values))


def _apply_values(ctx: AdminContext, plan: RevertPlan, obj: Any) -> None:
    """Restore recorded values: editable ones via the form, rest via setattr."""
    admin = _admin_of(plan)
    by_name = {spec.name: spec for spec in admin.get_form_specs()}
    editable = {name for name, spec in by_name.items() if spec.is_editable()}
    values = {name: value for name, value in plan.values.items() if name in by_name}
    form_values = {name: value for name, value in values.items() if name in editable}
    if form_values:
        bound = _bind_values(plan, ctx, form_values)
        if not bound.is_valid:
            first = next(iter(bound.errors.values()), ["invalid values"])[0]
            plan.blocked.append(
                BlockedField(
                    name="",
                    label="",
                    reason=f"the form now rejects the recorded values ({first}).",
                )
            )
            raise RevertBlocked("Recorded values no longer pass validation.", [plan])
        missing = [name for name in form_values if name not in bound.cleaned]
        if missing:
            plan.blocked.extend(
                BlockedField(name=name, label=name, reason="not editable for you.")
                for name in missing
            )
            raise RevertBlocked("Some fields are not editable for you.", [plan])
        bound.apply_to(obj)
    # Read-only fields (e.g. JSON columns) are not user-editable, so their
    # recorded value is assigned directly; the payload is trustworthy and the
    # flush still enforces column constraints.
    for name, value in values.items():
        if name not in editable:
            setattr(obj, name, value)


def _admin_of(plan: RevertPlan) -> ModelAdmin:
    if plan.admin is None:  # pragma: no cover - blocked entries never mutate
        raise RuntimeError("RevertPlan without a ModelAdmin cannot be applied.")
    return plan.admin


async def _mutate(ctx: AdminContext, plan: RevertPlan) -> None:
    """Stage the reverse change on the session (no flush)."""
    admin = _admin_of(plan)
    entry = plan.entry
    if entry.op == "create":
        await ctx.db.delete(plan.target)
        return
    if entry.op == "delete":
        obj = admin.model()
        setattr(obj, sa.inspect(admin.model).primary_key[0].key, plan.pk_value)
        _apply_values(ctx, plan, obj)
        ctx.db.add(obj)
        return
    _apply_values(ctx, plan, plan.target)


def _bind_revert_context(ctx: AdminContext, *, reverts_id: int, reason: str) -> None:
    ctx.db.info[CTX_KEY] = replace(
        from_request(ctx.request),
        source="revert",
        reverts_id=reverts_id,
        reason=reason[:500],
    )


async def revert_plans(
    ctx: AdminContext,
    plans: list[RevertPlan],
    *,
    force: bool,
    reverts_id: int,
    reason: str,
) -> None:
    """Apply a set of plans in one transaction (all or nothing)."""
    for plan in plans:
        if plan.noop:
            continue
        if plan.blocked:
            raise RevertBlocked("This entry cannot be reverted.", [plan])
        if plan.conflicts and not force:
            raise RevertConflict("The row changed since this entry was recorded.", [plan])
    _bind_revert_context(ctx, reverts_id=reverts_id, reason=reason)
    for plan in plans:
        if plan.noop:
            continue
        await _mutate(ctx, plan)
    try:
        async with ctx.db.begin_nested():
            await ctx.db.flush()
    except IntegrityError as exc:
        await ctx.db.rollback()
        raise RevertBlocked(
            f"Could not revert: the database rejected the change ({exc.orig}).",
            plans,
        ) from exc
    except StaleDataError as exc:
        await ctx.db.rollback()
        raise RevertConflict(
            "The row changed while the revert was being applied; try again.",
            plans,
        ) from exc


# -- views ------------------------------------------------------------------


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
        raise RuntimeError("AdminContext without a ModelAdmin on a model route.")
    try:
        pk_value = admin.parse_pk(pk)
    except LookupError:
        return _not_found(ctx.request, f"Invalid id {pk!r}.")
    if not ctx.user.has_perm(HISTORY_RESTORE_PERMISSION):
        return _forbidden(ctx.request, "You do not have permission to restore changes.")
    try:
        plan = await build_plan(ctx, admin, pk_value, entry_id)
    except LookupError as exc:
        return _not_found(ctx.request, str(exc))
    action = OP_PERMISSION.get(plan.entry.op, "change")
    if not getattr(admin, f"has_{action}_permission")(ctx):
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
        await revert_plans(
            ctx,
            [plan],
            force=force,
            reverts_id=plan.entry.id,
            reason=f"Revert of entry #{plan.entry.id}",
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
    from obox_builder.contrib.admin.site import site

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
        plan = await _plan_for_change_set(ctx, site, entry, allowed=allowed)
        plan.blocked = [
            replace(item, model_label=entry.model_label, object_repr=entry.object_repr)
            for item in plan.blocked
        ]
        plan.conflicts = [
            replace(item, model_label=entry.model_label, object_repr=entry.object_repr)
            for item in plan.conflicts
        ]
        plans.append(plan)
    form = await ctx.request.form()
    token = form.get("csrf_token")
    await ensure_csrf(ctx.request, token if isinstance(token, str) else None)
    force = form.get("force") == "1"
    back_url = f"/admin/history/{change_set_id}"
    try:
        await revert_plans(
            ctx,
            plans,
            force=force,
            reverts_id=max(row.id for row in rows),
            reason=f"Revert of change set {change_set_id}",
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


def _entry_blocked(entry: RowChangeEntry, reason: str) -> RevertPlan:
    return RevertPlan(
        admin=None,
        entry=entry,
        pk_value=None,
        blocked=[BlockedField(name="", label=entry.object_repr, reason=reason)],
    )


async def _plan_for_change_set(
    ctx: AdminContext,
    site: Any,
    entry: RowChangeEntry,
    *,
    allowed: frozenset[str] | None,
) -> RevertPlan:
    if allowed is not None and entry.model_label not in allowed:
        return _entry_blocked(entry, "the model is not visible to you.")
    model = model_for_label(entry.model_label)
    admin = site.get_model_admin_for(model) if model is not None else None
    if admin is None:
        return _entry_blocked(entry, "the model is no longer registered in the admin.")
    try:
        pk_value = admin.parse_pk(entry.object_pk)
    except LookupError:
        pk_value = entry.object_pk
    try:
        plan = await plan_from_row(ctx, admin, pk_value, entry)
    except Exception:  # noqa: BLE001 - a plan failure must not abort the report
        logger.exception("Failed to plan revert of entry #%d.", entry.id)
        return RevertPlan(
            admin=admin,
            entry=entry,
            pk_value=pk_value,
            blocked=[BlockedField(name="", label=entry.object_repr, reason="could not evaluate.")],
        )
    action = OP_PERMISSION.get(entry.op, "change")
    if not getattr(admin, f"has_{action}_permission")(ctx):
        plan.blocked.append(
            BlockedField(
                name="",
                label=entry.object_repr,
                reason=f"you lack '{admin.permission_name(action)}'.",
            )
        )
    return plan


__all__ = [
    "BlockedField",
    "Conflict",
    "RevertBlocked",
    "RevertConflict",
    "RevertPlan",
    "blocked_fields",
    "build_plan",
    "revert_change_set",
    "revert_entry",
]
