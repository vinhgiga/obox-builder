"""Adapt ModelAdmin querysets and permission hooks to import resources."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.imports.resources import ImportResource
from obox_builder.fields import RecordContext


class AdminImportResource(ImportResource):
    def __init__(self, ctx: AdminContext) -> None:
        if ctx.admin is None:
            raise ValueError("A model admin is required.")
        self.ctx, self.admin = ctx, ctx.admin
        super().__init__(
            self.admin.model,
            ctx.user,
            keys=self.admin.import_keys,
            specs=self.admin.get_form_specs(),
        )

    def queryset(self) -> sa.Select[Any]:
        return self.admin.get_queryset(self.ctx)

    def can_add(self) -> bool:
        return self.admin.has_add_permission(self.ctx)

    def can_change(self, obj: Any = None) -> bool:
        return self.admin.has_change_permission(self.ctx, obj)

    def context(self, obj: Any = None) -> RecordContext:
        return RecordContext(obj=obj, user=self.ctx.user, extra=self.ctx.extra)
