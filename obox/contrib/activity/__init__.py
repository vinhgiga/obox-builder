"""Optional, permission-aware record activity reads."""

from collections.abc import Callable
from typing import Any

from obox._context import Context
from obox._runtime import _Runtime
from obox.core.exceptions import ImproperlyConfigured


class Activity:
    requires_resources = True
    name = "activity"
    requires: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        identities: Callable[[set[str], Context], Any] | None = None,
    ) -> None:
        self.identities = identities

    async def snapshot(self, builder: Any, obj: Any, ctx: Context) -> dict[str, Any]:
        from obox.contrib.activity.services import snapshot

        return await snapshot(builder, obj, ctx)

    async def record(
        self, builder: Any, action: str, obj: Any, before: dict[str, Any] | None, ctx: Context
    ) -> None:
        from obox.contrib.activity.services import record

        await record(self, builder, action, obj, before, ctx)

    def _attach(self, app: _Runtime) -> None:
        from obox.contrib.activity.routes import build_router

        if hasattr(self, "app"):
            raise ImproperlyConfigured("Activity is already installed.")
        self.app = app
        for app_label in app.apps:
            app.include_router(build_router(self, app_label))
