"""Explicit collection writes through the ordinary authorized resource services."""

from collections.abc import Awaitable, Callable
from typing import Any, Literal

import sqlalchemy as sa

from obox_builder.builder.conditional import check_match
from obox_builder.builder.fields import schema, validate
from obox_builder.builder.query import invalid
from obox_builder.context import Context
from obox_builder.errors import NotFound, PermissionDenied, ResourceConflict


async def mutate(
    service: Any,
    id: str,
    field: str,
    record_ids: list[str],
    conditions: dict[str, str],
    mode: Literal["add", "replace", "remove"],
    ctx: Context,
    precondition: Callable[[Any], Awaitable[None]] | None,
) -> Any:
    from obox_builder.builder.api import record_etag

    b, app = service.builder, service.app
    if field not in b.api.write_relationships:
        raise NotFound("Relationship is unavailable.")
    relation = sa.inspect(b.model).relationships[field]
    registration = app._registrations.get(relation.mapper.class_)
    if registration is None:
        raise NotFound("Relationship target is unavailable.")
    target = registration.builder
    if not target.api.enabled or not {"retrieve", "update"}.issubset(target.api.operations):
        raise PermissionDenied("The relationship target is not writable.")
    other = app.service(target.model)
    async with service._mutation(ctx, conditional=True):
        parent = await service._get(id, ctx, "update")
        if precondition is not None:
            await precondition(parent)
        allowed = b.policy.write_fields("update", ctx, obj=parent)
        if allowed is not None and field not in allowed:
            raise PermissionDenied("Relationship is not writable.")
        identity = schema("RelationshipIdentity", target.specs, (target.pk_name,))
        keys = [validate(identity, {target.pk_name: key})[target.pk_name] for key in record_ids]
        if len(set(keys)) != len(keys):
            raise invalid("record_ids", "Choose unique record IDs.")
        await ctx.session.refresh(parent, attribute_names=[field])
        collection = getattr(parent, field)
        existing = {getattr(child, target.pk_name): child for child in collection}
        desired = set(keys) if mode == "replace" else set(existing)
        if mode == "add":
            desired.update(keys)
        elif mode == "remove":
            desired.difference_update(keys)
        added, removed = desired - existing.keys(), existing.keys() - desired
        affected = set(keys) | added | removed
        if b.api.max_batch is not None and len(affected) > b.api.max_batch:
            raise invalid("record_ids", "Relationship write exceeds the configured batch maximum.")
        if set(conditions) - {str(key) for key in affected}:
            raise invalid("if_matches", "Conditions must refer to affected records.")
        targets = {}
        for key in sorted(affected, key=str):
            child = await other._get(key, ctx, "update")
            await other.authorize("retrieve", ctx, child)
            if not await ctx.session.scalar(
                other._statement("retrieve", ctx).where(target.pk == key).exists().select()
            ):
                raise PermissionDenied("A related record is unavailable.")
            reverse = target.policy.write_fields("update", ctx, obj=child)
            if (
                relation.secondary is not None
                and reverse is not None
                and (not relation.back_populates or relation.back_populates not in reverse)
            ):
                raise PermissionDenied("The reverse relationship is not writable.")
            token = conditions.get(str(key))
            if target.version_column is not None:
                check_match(
                    [] if token is None else [token], await record_etag(target, other, child, ctx)
                )
            elif token is not None:
                raise invalid("if_matches", "The relationship target is not versioned.")
            targets[key] = child
        fk = None
        parent_value = None
        if relation.secondary is None:
            if len(relation.local_remote_pairs) != 1:
                raise invalid("field", "Composite relationship joins are not supported.")
            local, remote = relation.local_remote_pairs[0]
            fk = sa.inspect(target.model).get_property_by_column(remote).key
            parent_value = getattr(parent, sa.inspect(b.model).get_property_by_column(local).key)
            if removed and not remote.nullable:
                raise invalid("record_ids", "This relationship cannot be cleared.")
            if any(getattr(targets[key], fk) not in (None, parent_value) for key in added):
                raise ResourceConflict(
                    "Remove the existing parent relationship before reassignment."
                )
        if relation.secondary is not None:
            for key in sorted(removed, key=str):
                collection.remove(existing[key])
            for key in keys:
                if key in added:
                    collection.append(targets[key])
        for key in sorted(added | removed, key=str):
            changes = {} if fk is None else {fk: parent_value if key in added else None}
            await other.update(key, changes, ctx=ctx, touch=fk is None)
        if not added and not removed:
            return parent
        parent = await service.update(id, {}, ctx=ctx, touch=True)
        activity = app.get_plugin("activity")
        if activity is not None:
            from obox_builder.contrib.activity.models import Event
            from obox_builder.contrib.activity.services import ownership

            ctx.session.add(
                Event(
                    **await ownership(activity, b, parent, ctx),
                    kind="revision",
                    action="update",
                    source=ctx.source,
                    actor_id=str(ctx.principal.id),
                    changes=[
                        {
                            "field": field,
                            "kind": "reference",
                            "before": sorted(str(key) for key in existing),
                            "after": sorted(str(key) for key in desired),
                        }
                    ],
                )
            )
        return parent
