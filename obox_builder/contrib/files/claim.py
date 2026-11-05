"""Claim and release attachments in the owning record's transaction."""

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapper, object_session
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.base import NO_VALUE

from obox_builder.contrib.files.fields import changed, initialized
from obox_builder.contrib.files.models import File
from obox_builder.errors import BuilderValidationError, ImproperlyConfigured


def synchronize(
    mapper: Mapper[Any], connection: Connection, target: Any, *, delete: bool = False
) -> None:
    rules = {
        relation.key: relation.info["obox_files"]
        for relation in mapper.relationships
        if "obox_files" in relation.info
    }
    if not rules:
        return
    session = object_session(target)
    ctx = session.info.get("obox.context") if session is not None else None
    if ctx is None or ctx._owner.get_plugin("files") is None:
        raise ImproperlyConfigured(
            "Modify file fields inside an Obox unit of work with Files installed."
        )
    pk = mapper.primary_key[0]
    owner = connection.scalar(
        sa.select(
            sa.cast(
                sa.literal(getattr(target, mapper.get_property_by_column(pk).key), type_=pk.type),
                sa.String,
            )
        )
    )
    actor = getattr(ctx.principal, "id", None)
    table = sa.inspect(File).local_table
    for field, rule in rules.items():
        if (
            not delete
            and sa.inspect(target).persistent
            and sa.inspect(target).attrs[field].loaded_value is NO_VALUE
        ):
            continue
        value = [] if delete else getattr(target, field)
        ids = [file.id for file in value]
        if len(set(ids)) != len(ids):
            raise BuilderValidationError({field: ["Use a list of unique file IDs."]})
        if not delete:
            rule.count(field, len(ids))
        scope = (
            table.c.owner_type == target.__table__.fullname,
            table.c.field == field,
        )
        if ids:
            result = connection.execute(
                sa.update(File)
                .where(
                    *scope,
                    table.c.id.in_(ids),
                    table.c.status == "ready",
                    sa.or_(
                        table.c.owner_id == owner,
                        sa.and_(
                            table.c.owner_id.is_(None),
                            table.c.created_by == str(actor) if actor is not None else sa.false(),
                        ),
                    ),
                )
                .values(
                    owner_id=owner,
                    unclaimed_at=None,
                    position=sa.case(
                        {fid: index for index, fid in enumerate(ids)}, value=table.c.id
                    ),
                )
            )
            if result.rowcount != len(ids):
                raise BuilderValidationError(
                    {field: ["File unavailable, not ready or already attached."]}
                )
            for index, file in enumerate(value):
                rule.check(file.filename, file.size)
                set_committed_value(file, "owner_id", owner)
                set_committed_value(file, "position", index)
                set_committed_value(file, "unclaimed_at", None)
        detached = datetime.now(UTC)
        released = connection.execute(
            sa.update(File)
            .where(
                *scope,
                table.c.owner_id == owner,
                table.c.id.not_in(ids),
            )
            .values(owner_id=None, unclaimed_at=detached)
            .returning(table.c.id)
        )
        if session is not None:
            for fid in released.scalars():
                file = session.identity_map.get(
                    sa.inspect(File).identity_key_from_primary_key((fid,))
                )
                if file is not None:
                    set_committed_value(file, "owner_id", None)
                    set_committed_value(file, "unclaimed_at", detached)


@event.listens_for(Mapper, "mapper_configured")
def configured(mapper: Mapper[Any], cls: type[Any]) -> None:
    for relation in mapper.relationships:
        if "obox_files" in relation.info:
            attribute = getattr(cls, relation.key)
            for name in ("append", "remove", "bulk_replace"):
                event.listen(attribute, name, changed)
            event.listen(attribute, "init_collection", initialized)


@event.listens_for(Mapper, "after_insert")
@event.listens_for(Mapper, "after_update")
def saved(mapper: Mapper[Any], connection: Connection, target: Any) -> None:
    synchronize(mapper, connection, target)


@event.listens_for(Mapper, "after_delete")
def deleted(mapper: Mapper[Any], connection: Connection, target: Any) -> None:
    synchronize(mapper, connection, target, delete=True)
