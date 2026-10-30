"""The audit resource owns its stable permission identifiers."""

from dataclasses import replace
from uuid import UUID

import sqlalchemy as sa
from pydantic import BaseModel

from obox_builder.contrib.history.access import diff_mask
from obox_builder.contrib.history.models import RowChange
from obox_builder.contrib.history.service import model_for_label
from obox_builder.resources import (
    OperationContext,
    ReadOnlyModelResource,
    ResourcePolicy,
    capability,
    get_registry,
    operation,
    register,
)


def unrestricted(resource):
    return (
        type(resource.policy).scope is ResourcePolicy.scope
        and type(resource.policy).allows is ResourcePolicy.allows
        and type(resource).get_queryset is ReadOnlyModelResource.get_queryset
    )


class RestoreInput(BaseModel):
    force: bool = False


class RestoreSetInput(RestoreInput):
    change_set_id: UUID


class Restored(BaseModel):
    affected: int


class HistoryPolicy(ResourcePolicy):
    def scope(self, ctx, query, operation):
        registry = ctx.registry or get_registry()
        visible = []
        for resource in registry.resources.values():
            if not isinstance(resource, ReadOnlyModelResource) or resource.model is RowChange:
                continue
            context = ctx.for_resource(resource)
            if not resource.can_execute(context, "get"):
                continue
            pks = (
                resource.scoped_query(context, resource.operations["get"])
                .with_only_columns(sa.cast(resource.pk_column(), sa.String))
                .order_by(None)
            )
            target = RowChange.object_pk.in_(pks)
            if unrestricted(resource):
                target = sa.or_(target, RowChange.op == "delete")
            visible.append(sa.and_(RowChange.model_label == resource.scope_name, target))
        return query.where(sa.or_(*visible) if visible else sa.false())


@register()
class RowChangeResource(ReadOnlyModelResource[RowChange]):
    name = "history.rowChange"
    label = "Row change"
    model = RowChange
    fields = (
        "id",
        "occurred_at",
        "change_set_id",
        "op",
        "model_label",
        "object_pk",
        "object_repr",
        "changes",
        "schema_version",
        "row_version",
        "actor_id",
        "actor_label",
        "source",
        "request_id",
        "reason",
        "reverts_id",
    )
    export = capability("Can export row changes")
    ordering = ("-id",)

    policy = HistoryPolicy()

    async def record_visible(self, ctx, obj):
        ctx = replace(ctx, registry=ctx.registry or get_registry())
        model = model_for_label(obj.model_label)
        if model is None:
            return False
        try:
            resource = ctx.registry.for_model(model)
            context = ctx.for_resource(resource)
            target = await resource.resolve_target(
                context, resource.operations["get"], obj.object_pk
            )
        except LookupError:
            return (
                obj.op == "delete"
                and model in ctx.registry.models
                and unrestricted(ctx.registry.for_model(model))
            )
        return resource.can_execute(context, "get", target)

    async def resolve_target(self, ctx, spec, target):
        obj = await super().resolve_target(ctx, spec, target)
        if not await self.record_visible(ctx, obj):
            raise LookupError("History record is unavailable.")
        return obj

    async def serialize(self, ctx, obj):
        result = await super().serialize(ctx, obj)
        model = model_for_label(obj.model_label)
        mask = diff_mask(model, ctx)
        changes = {}
        for key, value in obj.changes.items():
            if isinstance(value, dict):
                changes[key] = {name: item for name, item in value.items() if mask.allows(name)}
            elif isinstance(value, list):
                changes[key] = [name for name in value if mask.allows(name)]
        data = result.model_dump(exclude_unset=True)
        data["changes"] = changes
        data["object_repr"] = f"#{obj.object_pk}"
        return self.read_schema.model_validate(data)

    @operation(scope="record", label="Can restore row changes")
    async def restore(self, ctx: OperationContext, obj: RowChange, data: RestoreInput) -> Restored:
        from obox_builder.contrib.history.revert import build_plan, revert_plans

        registry = ctx.registry or get_registry()
        model = model_for_label(obj.model_label)
        if model is None:
            raise LookupError("The affected resource is unavailable.")
        resource = registry.for_model(model)
        plan = await build_plan(ctx, resource, resource.parse_pk(obj.object_pk), obj.id)
        await revert_plans(
            ctx, [plan], force=data.force, reverts_id=obj.id, reason=f"Revert of entry #{obj.id}"
        )
        return Restored(affected=int(not plan.noop))

    @operation(permission="restore", label="Can restore row changes")
    async def restore_change_set(self, ctx: OperationContext, data: RestoreSetInput) -> Restored:
        from obox_builder.contrib.history.revert import (
            RevertBlocked,
            _entry_blocked,
            _plan_for_change_set,
            revert_plans,
        )
        from obox_builder.contrib.history.service import to_entry

        registry = ctx.registry or get_registry()
        rows = list(
            await ctx.session.scalars(
                sa.select(RowChange)
                .where(RowChange.change_set_id == data.change_set_id)
                .order_by(RowChange.id.desc())
            )
        )
        if not rows:
            raise LookupError("The change set is unavailable.")
        plans = []
        for row in rows:
            entry = to_entry(row)
            if not await self.record_visible(ctx, row):
                raise RevertBlocked(
                    "A record in the change set is unavailable.",
                    [_entry_blocked(entry, "The record is unavailable.")],
                )
            plans.append(await _plan_for_change_set(ctx, registry, entry, allowed=None))
        await revert_plans(
            ctx,
            plans,
            force=data.force,
            reverts_id=max(row.id for row in rows),
            reason=f"Revert of change set {data.change_set_id}",
        )
        return Restored(affected=sum(not plan.noop for plan in plans))
