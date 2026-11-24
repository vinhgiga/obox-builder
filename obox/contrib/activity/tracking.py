"""Capture audited changes on the same connection as each ORM write."""

from typing import Any

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import NO_VALUE, Mapper, Session, object_session

from obox.contrib.activity.models import Event
from obox.core.exceptions import ImproperlyConfigured
from obox.web.schemas import json_value


def context(session: Any) -> Any:
    if session is None:
        return None
    configured = session.info.get("obox.history")
    if configured is not None:
        return configured
    ctx = session.info.get("obox.runtime.context")
    if ctx is None or ctx._owner is None:
        return None
    activity = ctx._owner.feature("activity")
    return (activity, ctx.principal, ctx.source) if activity is not None else None


@event.listens_for(Mapper, "before_update")
@event.listens_for(Mapper, "before_delete")
def before(mapper: Any, connection: Any, target: Any) -> None:
    session = object_session(target)
    active = context(session)
    if session is None or active is None or type(target) not in active[0].fields:
        return
    fields = active[0].fields[type(target)]
    names = [name for name in fields if name in mapper.column_attrs]
    stmt = sa.select(*(getattr(type(target), name) for name in names)).where(
        *(
            column == value
            for column, value in zip(mapper.primary_key, sa.inspect(target).identity, strict=True)
        )
    )
    row = connection.execute(stmt).mappings().first() if names else {}
    snapshot = dict(row or {})
    for name in fields:
        if name in mapper.relationships:
            from obox.contrib.files.models import File

            snapshot[name] = [
                str(value)
                for value in connection.scalars(
                    sa.select(File.id)
                    .where(
                        File.owner_type == mapper.local_table.fullname,
                        File.owner_id == str(sa.inspect(target).identity[0]),
                        File.field == name,
                    )
                    .order_by(File.position, File.id)
                )
            ]
    session.info.setdefault("obox.history.before", {})[id(target)] = snapshot


def record(mapper: Any, connection: Any, target: Any, action: str) -> None:
    session = object_session(target)
    active = context(session)
    if session is None or active is None or type(target) not in active[0].fields:
        return
    activity, actor, source = active
    old = session.info.get("obox.history.before", {}).pop(id(target), {})
    new = {}
    if action != "delete":
        state = sa.inspect(target)
        for name in activity.fields[type(target)]:
            value = state.attrs[name].loaded_value
            if value is NO_VALUE:
                value = old.get(name)
            if name in mapper.relationships:
                value = (
                    [str(getattr(file, "id", file)) for file in value] if value is not None else []
                )
            new[name] = value
    changes = [
        dict(
            field=name,
            kind="files"
            if name in mapper.relationships
            else "reference"
            if mapper.column_attrs[name].columns[0].foreign_keys
            else "text",
            **({"before": json_value(old[name])} if name in old else {}),
            **({"after": json_value(new[name])} if name in new else {}),
        )
        for name in activity.fields[type(target)]
        if action != "update" or json_value(old.get(name)) != json_value(new.get(name))
    ]
    if changes:
        key = mapper.get_property_by_column(mapper.primary_key[0]).key
        connection.execute(
            sa.insert(Event).values(
                resource_key=mapper.local_table.fullname,
                record_id=str(getattr(target, key)),
                kind="revision",
                action=action,
                changes=changes,
                actor_id=str(actor.id) if getattr(actor, "id", None) is not None else None,
                source=source,
            )
        )


@event.listens_for(Mapper, "after_insert")
def inserted(mapper: Any, connection: Any, target: Any) -> None:
    record(mapper, connection, target, "create")


@event.listens_for(Mapper, "after_update")
def updated(mapper: Any, connection: Any, target: Any) -> None:
    record(mapper, connection, target, "update")


@event.listens_for(Mapper, "after_delete")
def deleted(mapper: Any, connection: Any, target: Any) -> None:
    record(mapper, connection, target, "delete")


@event.listens_for(Session, "do_orm_execute")
def bulk(execute_state: Any) -> None:
    active = context(execute_state.session)
    if active is None or not (
        execute_state.is_update or execute_state.is_delete or execute_state.is_insert
    ):
        return
    if any(mapper.class_ in active[0].fields for mapper in execute_state.all_mappers):
        raise ImproperlyConfigured(
            "Bulk writes bypass row history; use ORM instances for tracked models."
        )
