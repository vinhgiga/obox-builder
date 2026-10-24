from __future__ import annotations

from obox_builder.apps import AppConfig

from tests.support import events


class BlogConfig(AppConfig):
    label = "blog"
    verbose_name = "Blog"

    def ready(self) -> None:
        events.RECORDS.append("blog.ready")


class BlogAliasConfig(AppConfig):
    label = "blog_alias"
    default = False
