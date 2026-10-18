"""Application config for the row-history app."""

from __future__ import annotations

from typing import ClassVar

from obox_builder.apps import AppConfig


class HistoryConfig(AppConfig):
    name = "obox_builder.contrib.history"
    label = "history"
    verbose_name = "History"
    permissions: ClassVar[list[tuple[str, str]]] = [
        ("view_row_change", "Can view row changes"),
    ]

    def ready(self) -> None:
        from obox_builder.contrib.history import checks  # noqa: F401 (registers checks)
        from obox_builder.contrib.history.capture import ensure_listeners

        ensure_listeners()
