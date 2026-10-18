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
        ("restore_row_change", "Can restore reverted row changes"),
        ("export_row_change", "Can export row changes"),
    ]

    def ready(self) -> None:
        from obox_builder.contrib.history import checks  # noqa: F401 (registers checks)
        from obox_builder.contrib.history.capture import ensure_listeners
        from obox_builder.contrib.history.immutability import install_trigger_receiver
        from obox_builder.core.signals import post_migrate

        ensure_listeners()
        post_migrate.connect(
            install_trigger_receiver,
            dispatch_uid="obox_builder.contrib.history.install_trigger",
        )
