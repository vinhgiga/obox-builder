"""Shared account administration contributes an application to the catalog."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import Request

from obox._exceptions import N_
from obox._i18n import translate
from obox.apps import AppConfig

if TYPE_CHECKING:
    from obox._context import Context
    from obox._runtime import _Runtime


class AuthConfig(AppConfig):
    name = "obox.contrib.auth"
    label = "auth"
    verbose_name = N_("Users and roles")

    async def get_navigation(
        self, obox: _Runtime, request: Request, ctx: Context
    ) -> list[dict[str, Any]]:
        if not getattr(ctx.principal, "is_superuser", False):
            return []
        auth = obox.auth
        return [
            {"label": translate(request, "Users"), "url": auth.users_url(request), "kind": "page"},
            {"label": translate(request, "Roles"), "url": auth.roles_url(request), "kind": "page"},
        ]

    def ready(self) -> None:
        from obox.conf import settings
        from obox.contrib.auth import Auth

        options = dict(getattr(settings, "AUTH", {}))
        options.setdefault("secure", not settings.DEBUG)
        from functools import partial

        self._service_factory = partial(Auth, **options)
