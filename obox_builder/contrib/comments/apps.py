"""Application config for row comments."""

from __future__ import annotations

from typing import ClassVar

from obox_builder.apps import AppConfig


class CommentsConfig(AppConfig):
    name = "obox_builder.contrib.comments"
    label = "comments"
    verbose_name = "Comments"
    permissions: ClassVar[list[str]] = [
        "comments.rowComment.read",
        "comments.rowComment.create",
        "comments.rowComment.moderate",
        "comments.rowComment.export",
    ]

    def ready(self) -> None:
        from obox_builder.contrib.comments import checks  # noqa: F401 (registers checks)
