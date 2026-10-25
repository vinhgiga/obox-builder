"""Administrative token inspection and revocation without secret disclosure."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from obox_builder.contrib.admin.actions import action
from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.options import ModelAdmin
from obox_builder.core.utils.time import utcnow


class ApiTokenAdmin(ModelAdmin):
    list_display = ("id", "name", "user_id", "token_prefix", "expires_at", "revoked_at")
    fields = (
        "name",
        "user_id",
        "token_prefix",
        "scopes",
        "expires_at",
        "last_used_at",
        "last_used_ip",
        "revoked_at",
    )
    readonly_fields = fields
    search_fields = ("name", "token_prefix")
    actions = ("revoke_tokens",)

    def has_add_permission(self, ctx: AdminContext) -> bool:
        return False

    def has_change_permission(self, ctx: AdminContext, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, ctx: AdminContext, obj: Any = None) -> bool:
        return False

    def has_revoke_permission(self, ctx: AdminContext, obj: Any = None) -> bool:
        return bool(ctx.user.has_perm(self.permission_name("change")))

    @action(description="Revoke selected tokens", permissions=("revoke",))
    async def revoke_tokens(self, ctx: AdminContext, queryset: sa.Select[Any]) -> None:
        for token in (await ctx.db.scalars(queryset)).unique():
            if token.revoked_at is None:
                token.revoked_at = utcnow()
