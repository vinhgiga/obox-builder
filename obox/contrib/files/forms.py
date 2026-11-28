"""Attachment selection for the Files feature."""

from typing import Any

import sqlalchemy as sa

from obox.contrib.files.models import File
from obox.core.exceptions import ImproperlyConfigured
from obox.forms import ChoiceField, SelectMultiple
from obox.runtime.context import Context


async def attachment_control(model: type[Any], name: str, obj: Any, ctx: Context) -> ChoiceField:
    files = ctx._owner.feature("files") if ctx._owner is not None else None
    if files is None:
        raise ImproperlyConfigured("Install Files before editing attachments.")
    mapper = sa.inspect(model)
    key = (
        getattr(obj, mapper.get_property_by_column(mapper.primary_key[0]).key)
        if obj is not None
        else None
    )
    key = str(key) if key is not None else None
    owner = model.__table__.fullname
    await files.owner(owner, key, name, ctx, write=True)
    query = sa.select(File).where(
        File.owner_type == owner,
        File.field == name,
        File.status == "ready",
        sa.or_(
            sa.and_(File.owner_id.is_(None), File.created_by == str(ctx.principal.id)),
            File.owner_id == key if key is not None else sa.false(),
        ),
    )
    choices = [(str(file.id), file.filename) for file in await ctx.session.scalars(query)]
    return ChoiceField(
        required=bool(files.rule(owner, name).min_count),
        choices=choices,
        widget=SelectMultiple,
    )
