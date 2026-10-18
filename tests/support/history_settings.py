from __future__ import annotations

from tests.support.auth_settings import Settings as AuthSettings


class Settings(AuthSettings):
    INSTALLED_APPS: list[str] = [
        "obox_builder.contrib.history",
        "obox_builder.contrib.auth",
        "obox_builder.contrib.admin",
        "tests.support.apps.shop",
        "tests.support.apps.warehouse",
    ]
