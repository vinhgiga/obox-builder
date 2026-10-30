"""Application config for row comments."""

from __future__ import annotations

from obox_builder.apps import AppConfig


class CommentsConfig(AppConfig):
    name = "obox_builder.contrib.comments"
    label = "comments"
    verbose_name = "Comments"

    def ready(self) -> None:
        from obox_builder.contrib.comments import checks  # noqa: F401 (registers checks)
