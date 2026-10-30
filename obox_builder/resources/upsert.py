"""Atomic upserts against explicitly declared, database-enforced import keys."""

from dataclasses import replace
from typing import Any, cast

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, create_model
from sqlalchemy.exc import IntegrityError

from obox_builder.exceptions import ImproperlyConfigured, PermissionDenied
from obox_builder.forms import build_schema
from obox_builder.resources.context import OperationContext
from obox_builder.resources.errors import ResourceConflict, ResourceValidationError
from obox_builder.resources.revision import record_revision


def validate_keys(resource: Any) -> None:
    keys = resource.import_keys
    if len(set(keys)) != len(keys) or any(
        key not in resource.specs or resource.specs[key].column is None for key in keys
    ):
        raise ImproperlyConfigured("Import keys must be distinct published columns.")
    table = sa.inspect(resource.model).local_table
    unique = [set(table.primary_key.columns.keys())]
    unique += [
        set(c.columns.keys()) for c in table.constraints if isinstance(c, sa.UniqueConstraint)
    ]
    unique += [
        set(i.columns.keys())
        for i in table.indexes
        if i.unique
        and all(isinstance(expression, sa.Column) for expression in i.expressions)
        and not any(options.get("where") is not None for options in i.dialect_options.values())
    ]
    if set(keys) not in unique:
        raise ImproperlyConfigured("Import keys need an unconditional database unique constraint.")


def schema(resource: Any) -> type[BaseModel]:
    keys = build_schema(
        [
            replace(
                resource.specs[key], required=True, editable=True, readonly=False, primary_key=False
            )
            for key in resource.import_keys
        ],
        name=resource.name.replace(".", "_") + "Match",
    )
    row = create_model(
        resource.name.replace(".", "_") + "UpsertRow",
        __config__=ConfigDict(extra="forbid"),
        match=(keys, ...),
        data=(resource.update_schema, ...),
        revision=(str | None, None),
    )
    return create_model(
        resource.name.replace(".", "_") + "Upsert",
        __config__=ConfigDict(extra="forbid"),
        items=(
            cast(Any, list.__class_getitem__(row)),
            Field(min_length=1, max_length=resource.max_batch),
        ),
    )


async def execute(resource: Any, ctx: OperationContext, data: Any) -> Any:
    if not all(resource.can_execute(ctx, action) for action in ("get", "create", "update")):
        raise PermissionDenied("Upsert requires read, create and update access.")
    prepared = []
    seen = set()
    # Stable lock order avoids cross-request deadlocks for overlapping batches.
    rows = sorted(enumerate(data.items), key=lambda pair: pair[1].match.model_dump_json())
    for index, row in rows:
        match = row.match.model_dump()
        signature = row.match.model_dump_json()
        if signature in seen or any(value is None for value in match.values()):
            raise ResourceValidationError({"items": ["Upsert keys must be non-null and unique."]})
        seen.add(signature)
        condition = sa.and_(
            *(getattr(resource.model, key) == value for key, value in match.items())
        )
        query = resource.scoped_query(ctx, resource.operations["get"]).where(condition)
        target = await ctx.session.scalar(query.with_for_update(of=resource.model))
        values = row.data.model_dump(exclude_unset=True)
        if set(values) & set(match):
            raise ResourceValidationError({"data": ["Match keys cannot also be assigned."]})
        if target is None:
            if (
                await ctx.session.scalar(sa.select(resource.pk_column()).where(condition))
                is not None
            ):
                raise LookupError("The matching record is unavailable.")
            if row.revision is not None:
                raise ResourceConflict("The matching record no longer exists.")
            if any(not resource.specs[key].is_editable() for key in match):
                raise ResourceValidationError({"match": ["Read-only keys cannot create records."]})
            values = {**match, **values}
            await resource.bind(ctx, values)
        else:
            target = await resource.resolve_target(ctx, resource.operations["update"], target)
            if not resource.can_execute(ctx, "get", target) or not resource.can_execute(
                ctx, "update", target
            ):
                raise PermissionDenied("The matching record cannot be updated.")
            current = (await resource.serialize(ctx, target)).model_dump(
                mode="json", exclude_unset=True
            )
            if row.revision != record_revision(current, getattr(target, "version", None)):
                raise ResourceConflict("The matching record changed; supply its current revision.")
            await resource.bind(ctx, values, target)
        prepared.append((index, target, values))
    output = {}
    async with ctx.session.begin_nested():
        for index, target, values in prepared:
            try:
                result = await resource.execute(
                    "create" if target is None else "update", ctx, values, target=target
                )
            except IntegrityError as exc:
                raise ResourceConflict(
                    "An upsert key changed concurrently; reload before retrying."
                ) from exc
            output[index] = result
    return resource.batch_schema(items=[output[index] for index in range(len(data.items))])
