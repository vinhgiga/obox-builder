from __future__ import annotations

from tests.support.auth_settings import Settings as AuthSettings


class Settings(AuthSettings):
    INSTALLED_APPS: list[str] = [
        "obox_builder.contrib.events",
        "tests.support.apps.warehouse",
    ]
