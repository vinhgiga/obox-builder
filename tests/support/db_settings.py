from __future__ import annotations

from obox_builder.conf import BaseSettings
from pydantic import SecretStr


class Settings(BaseSettings):
    SECRET_KEY: SecretStr = SecretStr("x" * 48)
    DATABASE_URL: str = "sqlite+aiosqlite:///:memory:"
    INSTALLED_APPS: list[str] = ["tests.support.apps.shop"]
