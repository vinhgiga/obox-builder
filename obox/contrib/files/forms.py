"""Attachment form choices scoped to the editing identity and current record."""

from typing import Any

import sqlalchemy as sa

from obox._exceptions import BuilderValidationError
from obox.contrib.files.models import File
from obox.core.exceptions import ValidationError
from obox.db import _session
from obox.forms.relations import ModelMultipleChoiceField


class AttachmentField(ModelMultipleChoiceField):
    def __init__(self, model: type[Any], name: str, rule: Any, **kwargs: Any) -> None:
        self.owner_model, self.name, self.rule, self.instance = model, name, rule, None
        super().__init__(sa.select(File).where(sa.false()), required=bool(rule.min_count), **kwargs)

    async def _scope(self) -> None:
        async with _session() as session:
            ctx = session.info.get("obox._context")
            if ctx is None or ctx._owner is None:
                raise ValidationError("Attachment forms require an authenticated file context.")
            files = ctx._owner._feature("files")
            if files is None:
                raise ValidationError("Install file uploads before editing attachments.")
            mapper = sa.inspect(self.owner_model)
            key = getattr(
                self.instance, mapper.get_property_by_column(mapper.primary_key[0]).key, None
            )
            await files.owner(
                self.owner_model.__table__.fullname,
                str(key) if key is not None else None,
                self.name,
                ctx,
                write=True,
            )
            self.queryset = sa.select(File).where(
                File.owner_type == self.owner_model.__table__.fullname,
                File.field == self.name,
                File.status == "ready",
                sa.or_(
                    sa.and_(File.owner_id.is_(None), File.created_by == str(ctx.principal.id)),
                    File.owner_id == str(key) if key is not None else sa.false(),
                ),
            )

    async def prepare(self) -> None:
        await self._scope()
        await super().prepare()

    async def clean(self, value: Any) -> Any:
        await self._scope()
        result = await super().clean(value)
        try:
            self.rule.count(self.name, len(result))
        except BuilderValidationError as exc:
            raise ValidationError(exc.errors[self.name]) from exc
        return result
