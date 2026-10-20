"""Application config for row comments."""

from __future__ import annotations

from typing import ClassVar

from obox_builder.apps import AppConfig


class CommentsConfig(AppConfig):
    name = "obox_builder.contrib.comments"
    label = "comments"
    verbose_name = "Comments"
    permissions: ClassVar[list[tuple[str, str]]] = [
        ("view_row_comment", "Can view row comments"),
        ("add_row_comment", "Can add row comments"),
        ("moderate_row_comment", "Can moderate row comments"),
        ("export_row_comment", "Can export row comments"),
    ]

    def ready(self) -> None:
        from obox_builder.contrib.comments import checks  # noqa: F401 (registers checks)
