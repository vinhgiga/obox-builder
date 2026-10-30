"""Bounded cascade discovery and authorization without HTTP or presentation state."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import with_parent

from obox_builder.resources.context import OperationContext
from obox_builder.resources.registry import get_registry

MAX_OBJECTS = 1000


@dataclass
class DeleteEffect:
    key: str
    model: str
    label: str
    action: str
    depth: int
    obj: Any = None


@dataclass
class DeletionPlan:
    roots: list[Any]
    effects: dict[str, DeleteEffect] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    permissions: set[str] = field(default_factory=set)

    @property
    def allowed(self) -> bool:
        return not self.blockers and not self.permissions

    @property
    def fingerprint(self) -> str:
        values = [(item.key, item.action, item.label) for item in self.effects.values()]
        roots = sorted(
            (
                sa.inspect(type(obj)).local_table.fullname,
                [str(value) for value in sa.inspect(obj).identity],
            )
            for obj in self.roots
        )
        return hashlib.sha256(
            json.dumps([roots, sorted(values)], ensure_ascii=False).encode()
        ).hexdigest()

    @property
    def counts(self) -> dict[str, int]:
        return dict(
            Counter(effect.model for effect in self.effects.values() if effect.action == "delete")
        )


def _identity(table: sa.Table, values: dict[str, Any]) -> str:
    columns = list(table.primary_key) or list(table.c)
    return table.fullname + ":" + json.dumps([str(values[column.key]) for column in columns])


def _operation(constraint: Any, relationship: Any, obj: Any, *, secondary: bool = False) -> str:
    if relationship is not None and not relationship.viewonly:
        loaded = relationship.key not in sa.inspect(obj).unloaded
        if secondary and (not relationship.passive_deletes or loaded):
            return "unlink"
        if not secondary and "delete" in relationship.cascade:
            return "delete"
        if (
            not secondary
            and relationship.passive_deletes != "all"
            and (not relationship.passive_deletes or loaded)
        ):
            return (
                "update"
                if all(element.parent.nullable for element in constraint.elements)
                else "blocked"
            )
    action = (constraint.ondelete or "NO ACTION").upper()
    if action == "CASCADE":
        return "delete"
    if action == "SET NULL":
        return (
            "update"
            if all(element.parent.nullable for element in constraint.elements)
            else "blocked"
        )
    return "blocked"


async def collect_deletion(
    ctx: OperationContext, roots: list[Any], *, lock: bool = False
) -> DeletionPlan:
    plan = DeletionPlan(roots)
    if not roots:
        return plan
    registry = sa.inspect(type(roots[0])).registry
    models = {mapper.local_table: mapper.class_ for mapper in registry.mappers}
    pending: list[tuple[Any, dict[str, Any], Any, int]] = []
    visited: set[str] = set()
    protected: dict[str, str] = {}

    async def add(table: Any, values: dict[str, Any], obj: Any, action: str, depth: int) -> None:
        key = _identity(table, values)
        old = plan.effects.get(key)
        if old is not None and (old.action == "delete" or old.action == action):
            return
        if len(plan.effects) >= MAX_OBJECTS:
            if "Too many related records. Narrow the selection." not in plan.blockers:
                plan.blockers.append("Too many related records. Narrow the selection.")
            return
        label, model_name = table.name, table.name
        model = models.get(table)
        if model is not None and obj is not None:
            catalog = ctx.registry or get_registry()
            resource: Any = (
                ctx.resource
                if getattr(ctx.resource, "model", None) is model
                else catalog.models.get(model)
            )
            if resource is None:
                label = "Restricted record"
                if action in {"delete", "update"}:
                    plan.permissions.add(f"{action} {model.__name__}")
            else:
                model_ctx = ctx.for_resource(resource)
                model_name = resource.plural_name
                pk = resource.get_pk(obj)
                mutation = "delete" if action == "delete" else "update"
                spec = resource.operations.get(mutation) or resource.operations.get("get")
                scoped = None
                if spec is not None:
                    scoped = await ctx.session.scalar(
                        resource.scoped_query(model_ctx, spec)
                        .with_only_columns(resource.pk_column())
                        .where(resource.pk_column() == pk)
                        .order_by(None)
                    )
                visible = scoped is not None and resource.can_execute(model_ctx, "get", obj)
                text = str(obj)
                label = (
                    (f"#{pk}" if text.startswith("<") else text) if visible else "Restricted record"
                )
                if action in {"delete", "update"} and (
                    scoped is None or not resource.can_execute(model_ctx, mutation, obj)
                ):
                    plan.permissions.add(f"{mutation} {model_name.lower()}")
        plan.effects[key] = DeleteEffect(key, model_name, label, action, depth, obj)
        if action == "delete":
            pending.append((table, values, obj, depth))
        elif action == "blocked":
            protected[key] = f"{model_name}: {label} prevents deletion."

    for obj in roots:
        table = sa.inspect(type(obj)).local_table
        values = {column.key: getattr(obj, column.key) for column in table.c}
        await add(table, values, obj, "delete", 0)
    while pending:
        table, values, obj, depth = pending.pop(0)
        identity = _identity(table, values)
        if identity in visited:
            continue
        visited.add(identity)
        mapper = sa.inspect(type(obj)) if obj is not None else None
        relationships = list(mapper.relationships) if mapper is not None else []
        for relationship in relationships:
            if (
                relationship.viewonly
                or "delete" not in relationship.cascade
                or relationship.direction.name == "ONETOMANY"
            ):
                continue
            related_model = relationship.mapper.class_
            rows = (
                await ctx.session.scalars(
                    sa.select(related_model)
                    .where(with_parent(obj, getattr(type(obj), relationship.key)))
                    .limit(MAX_OBJECTS + 1)
                )
            ).unique()
            for child in rows:
                child_table = sa.inspect(type(child)).local_table
                child_values = {column.key: getattr(child, column.key) for column in child_table.c}
                await add(child_table, child_values, child, "delete", depth + 1)
        for related_table in table.metadata.tables.values():
            for constraint in related_table.foreign_key_constraints:
                if constraint.referred_table is not table:
                    continue
                relationship = next(
                    (
                        rel
                        for rel in relationships
                        if not rel.viewonly
                        and (
                            rel.secondary is related_table
                            and any(
                                local is element.column and remote is element.parent
                                for local, remote in rel.synchronize_pairs
                                for element in constraint.elements
                            )
                            or rel.secondary is None
                            and rel.direction.name == "ONETOMANY"
                            and any(
                                remote is element.parent
                                for _local, remote in rel.local_remote_pairs
                                for element in constraint.elements
                            )
                        )
                    ),
                    None,
                )
                action = _operation(
                    constraint,
                    relationship,
                    obj,
                    secondary=relationship is not None and relationship.secondary is related_table,
                )
                condition = sa.and_(
                    *(
                        element.parent == values[element.column.key]
                        for element in constraint.elements
                    )
                )
                model = models.get(related_table)
                stmt = (
                    sa.select(model if model is not None else related_table)
                    .where(condition)
                    .limit(MAX_OBJECTS + 1)
                )
                if lock:
                    stmt = stmt.with_for_update(of=model if model is not None else related_table)
                if model is not None:
                    rows = (await ctx.session.scalars(stmt)).unique()
                    entries = [
                        (
                            {column.key: getattr(child, column.key) for column in related_table.c},
                            child,
                        )
                        for child in rows
                    ]
                else:
                    entries = [
                        (dict(row), None) for row in (await ctx.session.execute(stmt)).mappings()
                    ]
                for child_values, child in entries:
                    await add(related_table, child_values, child, action, depth + 1)
    plan.blockers.extend(
        reason for key, reason in protected.items() if plan.effects[key].action != "delete"
    )
    return plan
