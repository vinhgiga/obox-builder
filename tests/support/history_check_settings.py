from __future__ import annotations

from tests.support.history_settings import Settings as HistorySettings


class Settings(HistorySettings):
    INSTALLED_APPS: list[str] = [
        "obox_builder.contrib.history",
        "obox_builder.contrib.auth",
        "obox_builder.contrib.admin",
        "tests.support.apps.shop",
        "tests.support.apps.warehouse",
        "tests.support.apps.badhistory",
    ]
