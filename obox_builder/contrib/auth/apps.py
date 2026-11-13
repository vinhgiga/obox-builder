"""Shared account administration contributes an application to the catalog."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import Request

from obox_builder.apps import AppConfig
from obox_builder.errors import N_
from obox_builder.i18n import translate

if TYPE_CHECKING:
    from obox_builder.application import Obox
    from obox_builder.context import Context


class AuthConfig(AppConfig):
    name = "obox_builder.contrib.auth"
    label = "auth"
    verbose_name = N_("Users and roles")

    async def get_navigation(
        self, obox: Obox, request: Request, ctx: Context
    ) -> list[dict[str, Any]]:
        if not getattr(ctx.principal, "is_superuser", False):
            return []
        auth = obox.auth
        return [
            {"label": translate(request, "Users"), "url": auth.users_url(request), "kind": "page"},
            {"label": translate(request, "Roles"), "url": auth.roles_url(request), "kind": "page"},
        ]
