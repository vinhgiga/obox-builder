"""Optional, permission-aware record activity reads."""

from collections.abc import Callable
from typing import Any

from obox_builder.application import Obox
from obox_builder.context import Context
from obox_builder.errors import ImproperlyConfigured


class Activity:
    name = "activity"
    requires: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        partition: Callable[[Context], str] | None = None,
        identities: Callable[[set[str], Context], Any] | None = None,
    ) -> None:
        self.partition = partition
        self.identities = identities

    def setup(self, app: Obox) -> None:
        from obox_builder.contrib.activity.routes import build_router

        if hasattr(self, "app"):
            raise ImproperlyConfigured("Activity is already installed.")
        self.app = app
        app.include_router(build_router(self))
