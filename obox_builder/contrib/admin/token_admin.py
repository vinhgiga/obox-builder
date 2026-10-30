"""Administrative token inspection and revocation without secret disclosure."""

from __future__ import annotations

from obox_builder.contrib.admin.options import ModelView


class ApiTokenAdmin(ModelView):
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
    actions = ("revoke",)
