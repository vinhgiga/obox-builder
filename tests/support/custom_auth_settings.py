from __future__ import annotations

from obox_builder.conf import BaseSettings
from pydantic import SecretStr


class Settings(BaseSettings):
    SECRET_KEY: SecretStr = SecretStr("custom-auth-settings-secret-key-012345")
    DATABASE_URL: str = "sqlite+aiosqlite:///:memory:"
    INSTALLED_APPS: list[str] = [
        "obox_builder.contrib.auth",
        "obox_builder.contrib.admin",
        "obox_builder.contrib.comments",
        "tests.support.apps.customusers",
    ]
    AUTH_USER_MODEL: str = "customusers.Account"
