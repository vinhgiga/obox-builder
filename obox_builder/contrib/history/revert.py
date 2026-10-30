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

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from obox_builder.contrib.history.context import CTX_KEY, resolve_context
from obox_builder.contrib.history.models import RowChange
from obox_builder.contrib.history.service import (
    RowChangeEntry,
    field_labels,
    model_for_label,
    to_entry,
)
from obox_builder.exceptions import PermissionDenied
from obox_builder.fields import FieldSpec
from obox_builder.forms import BoundForm, build_schema
from obox_builder.resources import (
    ModelResource,
    OperationContext,
    ReadOnlyModelResource,
    ResourceConflict,
    get_registry,
)

if TYPE_CHECKING:
    from obox_builder.contrib.history.access import DiffMask

logger = logging.getLogger("obox.history.audit")

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

    resource: ReadOnlyModelResource | None
    entry: RowChangeEntry
    pk_value: Any
    target: Any | None = None
    values: dict[str, Any] = field(default_factory=dict)
    blocked: list[BlockedField] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    #: True when the desired state already holds (nothing to do).
    noop: bool = False


class RevertError(ResourceConflict):
    """Base for refusal; carries the plans so the view can explain itself."""

    def __init__(self, message: str, plans: list[RevertPlan]) -> None:
        super().__init__(message)
        self.message = message
        self.plans = plans


class RevertBlocked(RevertError):
    """The entry (or change set) cannot be reverted at all."""


class RevertConflict(RevertError):
    """The row changed since the entry was recorded (``force`` can override)."""


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
    ctx: OperationContext,
    resource: ReadOnlyModelResource,
    pk_value: Any,
    entry_id: int,
    *,
    mask: DiffMask | None = None,
) -> RevertPlan:
    """Load one entry and work out how to revert it (``LookupError`` = 404)."""
    row = await ctx.session.get(RowChange, entry_id)
    if row is None or row.model_label != resource.scope_name or row.object_pk != str(pk_value):
        raise LookupError(f"History entry {entry_id} does not belong to this row.")
    return await plan_from_row(ctx, resource, pk_value, to_entry(row), mask=mask)


async def plan_from_row(
    ctx: OperationContext,
    resource: ReadOnlyModelResource,
    pk_value: Any,
    entry: RowChangeEntry,
    *,
    mask: DiffMask | None = None,
) -> RevertPlan:
    ctx = replace(ctx, resource=resource, obj=None)
    plan = RevertPlan(resource=resource, entry=entry, pk_value=pk_value)
    plan.target = await ctx.session.get(resource.model, pk_value)
    if plan.target is not None:
        action = {"update": "update", "delete": "create", "create": "delete"}[entry.op]
        operation_spec = resource.operations.get(action) or resource.operations["get"]
        plan.target = await resource.resolve_target(ctx, operation_spec, str(pk_value))
        ctx = replace(ctx, obj=plan.target)
    if not resource.can_execute(ctx, "get", plan.target):
        raise PermissionDenied("The affected resource is unavailable.")
    from obox_builder.contrib.history.access import diff_mask

    mask = mask or diff_mask(resource.model, ctx)
    specs = {spec.name: spec for spec in resource.specs.values()}
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


def _resource_of(plan: RevertPlan) -> ReadOnlyModelResource:
    if plan.resource is None:  # pragma: no cover - blocked entries never mutate
        raise RuntimeError("RevertPlan without a ReadOnlyModelResource cannot be applied.")
    return plan.resource


async def _mutate(ctx: OperationContext, plan: RevertPlan) -> None:
    resource = _resource_of(plan)
    context = replace(ctx.for_resource(resource), obj=plan.target)
    if plan.entry.op == "create":
        await resource.execute("delete", context, target=plan.target)
        return
    if not isinstance(resource, ModelResource):
        plan.blocked.append(
            BlockedField(
                name="",
                label="",
                reason="This resource requires its dedicated administration operations.",
            )
        )
        raise RevertBlocked("This resource does not support snapshot restoration.", [plan])
    action = "create" if plan.entry.op == "delete" else "update"
    if not resource.can_execute(context, action, plan.target):
        raise PermissionDenied("The affected operation is not allowed.")
    obj = resource.model() if action == "create" else plan.target
    if action == "create":
        setattr(obj, resource.pk_column().key, plan.pk_value)
    context = replace(context, obj=obj)
    values = {name: value for name, value in plan.values.items() if name in resource.specs}
    specs = [replace(resource.specs[name], readonly=False, editable=True) for name in values]
    for spec in specs:
        if not resource.field_allowed(context, spec, write=True):
            raise PermissionDenied("A recorded field can no longer be written.")
    bound = BoundForm.bind(
        specs,
        build_schema(specs, mode="revert"),
        values,
        base=context.record_context(values=values),
    )
    from obox_builder.resources.validation import validate_relations, validate_unique

    if bound.is_valid:
        await validate_relations(resource, context, bound)
        await validate_unique(resource.model, context, bound, None if action == "create" else obj)
    if not bound.is_valid:
        plan.blocked.extend(
            BlockedField(name=name, label=name, reason="; ".join(errors))
            for name, errors in bound.errors.items()
        )
        raise RevertBlocked("Recorded values no longer pass validation.", [plan])
    bound.apply_to(obj)
    await resource.save_model(context, obj, bound, change=action == "update")
    await resource.save_related(context, obj, bound, change=action == "update")


def _bind_revert_context(ctx: OperationContext, *, reverts_id: int, reason: str) -> None:
    ctx.session.info[CTX_KEY] = replace(
        resolve_context(ctx.session),
        actor_id=ctx.actor.id,
        actor_label=ctx.actor.username or "system",
        source="revert",
        reverts_id=reverts_id,
        reason=reason[:500],
    )


async def revert_plans(
    ctx: OperationContext, plans: list[RevertPlan], *, force: bool, reverts_id: int, reason: str
) -> None:
    registry = ctx.registry or get_registry()
    history = registry.for_model(RowChange)
    if not ctx.for_resource(history).can(history.operations["restore"].permission, effect="write"):
        raise PermissionDenied("Restoring history is not allowed.")
    for plan in plans:
        if plan.noop:
            continue
        if plan.blocked:
            raise RevertBlocked("This entry cannot be reverted.", [plan])
        if plan.conflicts and not force:
            raise RevertConflict("The row changed since this entry was recorded.", [plan])
        resource = _resource_of(plan)
        action = {"update": "update", "delete": "create", "create": "delete"}[plan.entry.op]
        if not resource.can_execute(ctx.for_resource(resource), action, plan.target):
            raise PermissionDenied("An affected operation is not allowed.")
    previous = ctx.session.info.get(CTX_KEY)
    try:
        async with ctx.session.begin_nested():
            _bind_revert_context(ctx, reverts_id=reverts_id, reason=reason)
            for plan in plans:
                if not plan.noop:
                    await _mutate(ctx, plan)
            await ctx.session.flush()
    except IntegrityError as exc:
        raise RevertBlocked("A database constraint prevented this restoration.", plans) from exc
    except StaleDataError as exc:
        raise RevertConflict("The row changed while the restore was being applied.", plans) from exc
    finally:
        if previous is None:
            ctx.session.info.pop(CTX_KEY, None)
        else:
            ctx.session.info[CTX_KEY] = previous


def _entry_blocked(entry: RowChangeEntry, reason: str) -> RevertPlan:
    return RevertPlan(
        resource=None,
        entry=entry,
        pk_value=None,
        blocked=[BlockedField(name="", label="Restricted record", reason=reason)],
    )


async def _plan_for_change_set(
    ctx: OperationContext,
    registry: Any,
    entry: RowChangeEntry,
    *,
    allowed: frozenset[str] | None,
) -> RevertPlan:
    if allowed is not None and entry.model_label not in allowed:
        return _entry_blocked(entry, "the model is not visible to you.")
    model = model_for_label(entry.model_label)
    resource = registry.models.get(model) if model is not None else None
    if resource is None:
        return _entry_blocked(entry, "the affected resource is no longer registered.")
    ctx = replace(ctx, resource=resource, obj=None)
    try:
        pk_value = resource.parse_pk(entry.object_pk)
    except LookupError:
        pk_value = entry.object_pk
    try:
        plan = await plan_from_row(ctx, resource, pk_value, entry)
    except Exception:  # noqa: BLE001 - a plan failure must not abort the report
        logger.exception("Failed to plan revert of entry #%d.", entry.id)
        return RevertPlan(
            resource=resource,
            entry=entry,
            pk_value=pk_value,
            blocked=[BlockedField(name="", label=entry.object_repr, reason="could not evaluate.")],
        )
    action = {"update": "update", "delete": "create", "create": "delete"}[entry.op]
    if not resource.can_execute(ctx, action, plan.target):
        plan.blocked.append(
            BlockedField(
                name="", label="Restricted record", reason=f"The {action} operation is not allowed."
            )
        )
    return plan
