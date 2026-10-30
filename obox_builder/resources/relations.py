"""Typed child collections with shared ownership, policy and transaction checks."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Union, cast

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, create_model

from obox_builder.exceptions import ImproperlyConfigured, PermissionDenied
from obox_builder.resources.context import OperationContext
from obox_builder.resources.errors import ResourceConflict, ResourceValidationError


@dataclass(frozen=True)
class Related:
    resource: str
    foreign_key: str
    min_num: int = 0
    max_num: int = 1000

    def validate(self, parent: Any, child: Any) -> None:
        if self.min_num < 0 or self.max_num < self.min_num:
            raise ImproperlyConfigured("Invalid related collection limits.")
        column = sa.inspect(child.model).columns.get(self.foreign_key)
        if column is None or not any(fk.column is parent.pk_column() for fk in column.foreign_keys):
            raise ImproperlyConfigured("Related.foreign_key must reference the parent primary key.")
        spec = child.specs.get(self.foreign_key)
        if spec is None or not spec.is_editable():
            raise ImproperlyConfigured("The child resource must publish its editable foreign key.")
        if column.unique and self.max_num != 1:
            raise ImproperlyConfigured("A unique related foreign key requires max_num=1.")


def _model(name: str, **fields: Any) -> type[BaseModel]:
    return create_model(name, __config__=ConfigDict(extra="forbid"), **fields)


def _without(schema: type[BaseModel], foreign_key: str, name: str) -> type[BaseModel]:
    return _model(
        name,
        **{
            key: (field.annotation, field)
            for key, field in schema.model_fields.items()
            if key != foreign_key
        },
    )


def _union(items: list[Any], discriminator: str) -> Any:
    if len(items) == 1:
        return items[0]
    union: Any = Union
    annotated: Any = Annotated
    return annotated[union[tuple(items)], Field(discriminator=discriminator)]


def schemas(parent: Any, method: str) -> tuple[type[BaseModel], type[BaseModel]]:
    variants = []
    outputs = []
    for name, relation in parent.related.items():
        child = parent._related_resources[name]
        prefix = parent.name.replace(".", "_") + name.title()
        output = _model(
            prefix + "Related",
            relation=(Literal.__getitem__(name), name),
            items=(cast(Any, list.__class_getitem__(child.read_schema)), ...),
            revision=(str, ...),
        )
        outputs.append(output)
        if method == "list_related":
            variants.append(_model(prefix + "Query", relation=(Literal.__getitem__(name), ...)))
            continue
        create = _model(
            prefix + "Add",
            action=(Literal["create"], ...),
            data=(
                _without(child.create_schema, relation.foreign_key, prefix + "Create"),
                ...,
            ),
        )
        update = _model(
            prefix + "Edit",
            action=(Literal["update"], ...),
            id=(str, ...),
            data=(
                _without(child.update_schema, relation.foreign_key, prefix + "Update"),
                ...,
            ),
        )
        delete = _model(prefix + "Remove", action=(Literal["delete"], ...), id=(str, ...))
        change = _union([create, update, delete], "action")
        variants.append(
            _model(
                prefix + "Changes",
                relation=(Literal.__getitem__(name), ...),
                revision=(str, ...),
                changes=(
                    cast(Any, list.__class_getitem__(change)),
                    Field(min_length=1, max_length=parent.max_selection),
                ),
            )
        )
    # RootModel keeps the discriminated JSON object intact for transport adapters.
    from pydantic import RootModel

    def root(items: list[Any], suffix: str) -> type[BaseModel]:
        annotation = _union(items, "relation")
        return create_model(
            parent.name.replace(".", "_") + suffix,
            __base__=cast(type[BaseModel], RootModel.__class_getitem__(annotation)),
        )

    return root(variants, method.title()), root(outputs, "RelatedResult")


async def snapshot(parent: Any, ctx: OperationContext, obj: Any, name: str) -> dict[str, Any]:
    relation = parent.related[name]
    child = parent._related_resources[name]
    child_ctx = ctx.for_resource(child)
    if not parent.can_execute(ctx, "get", obj) or not child.can_execute(child_ctx, "list"):
        raise PermissionDenied("The related collection cannot be read.")
    await parent.resolve_target(ctx, parent.operations["get"], obj)
    query = child.scoped_query(child_ctx, child.operations["list"]).where(
        getattr(child.model, relation.foreign_key) == parent.get_pk(obj)
    )
    rows = list((await ctx.session.scalars(query.order_by(child.pk_column()))).unique())
    rows = [row for row in rows if await child.record_visible(child_ctx, row)]
    if len(rows) > relation.max_num:
        raise ResourceConflict("The related collection exceeds its configured limit.")
    items = [
        (await child.serialize(child_ctx, row)).model_dump(mode="json", exclude_unset=True)
        for row in rows
    ]
    versions = [getattr(row, "version", None) for row in rows]
    revision = hashlib.sha256(
        json.dumps([items, versions], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {"relation": name, "items": items, "revision": revision}


async def edit(parent: Any, ctx: OperationContext, obj: Any, data: Any) -> dict[str, Any]:
    from obox_builder.resources.models import ModelResource

    relation = parent.related[data.relation]
    child = parent._related_resources[data.relation]
    if not isinstance(child, ModelResource):
        raise PermissionDenied("This related collection is read-only.")
    child_ctx = ctx.for_resource(child)
    fk = getattr(child.model, relation.foreign_key)
    parent_id = parent.get_pk(obj)
    # The dispatcher locks the parent first; all aggregate writers use this order.
    all_ids = list(
        await ctx.session.scalars(
            sa.select(child.pk_column())
            .where(fk == parent_id)
            .order_by(child.pk_column())
            .with_for_update(of=child.model)
        )
    )
    current = await snapshot(parent, ctx, obj, data.relation)
    if data.revision != current["revision"]:
        raise ResourceConflict("The related collection changed; reload before saving.")
    seen = set()
    prepared = []
    count = len(all_ids)
    for change in data.changes:
        target = None
        values = None
        if change.action != "create":
            identity = child.parse_pk(change.id)
            if identity in seen:
                raise ResourceValidationError({"changes": ["Duplicate child identifier."]})
            seen.add(identity)
            target = await child.resolve_target(
                child_ctx, child.operations[change.action], identity
            )
            if getattr(target, relation.foreign_key) != parent_id:
                raise LookupError("The related record is unavailable.")
        if not child.can_execute(child_ctx, change.action, target):
            raise PermissionDenied("A related record does not allow this operation.")
        if change.action == "delete":
            count -= 1
        else:
            values = change.data.model_dump(exclude_unset=True)
            if change.action == "create":
                values[relation.foreign_key] = parent_id
                count += 1
            await child.bind(child_ctx, values, target)
        prepared.append((change.action, target, values))
    if not relation.min_num <= count <= relation.max_num:
        raise ResourceValidationError(
            {"changes": ["Related row count is outside configured limits."]}
        )
    async with ctx.session.begin_nested():
        for action, target, values in sorted(prepared, key=lambda item: item[0] != "delete"):
            await child.execute(action, child_ctx, values, target=target)
    return await snapshot(parent, ctx, obj, data.relation)
