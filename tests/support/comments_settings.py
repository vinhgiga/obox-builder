from __future__ import annotations

from tests.support.admin_settings import Settings as AdminSettings


class Settings(AdminSettings):
    INSTALLED_APPS: list[str] = [
        "obox_builder.contrib.history",
        "obox_builder.contrib.comments",
        "obox_builder.contrib.auth",
        "obox_builder.contrib.admin",
        "obox_builder.contrib.savedviews",
        "tests.support.apps.shop",
        "tests.support.apps.commentsdemo",
    ]
