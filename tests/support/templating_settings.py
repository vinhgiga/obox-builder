from __future__ import annotations

from pathlib import Path

from obox_builder.conf import BaseSettings
from pydantic import SecretStr

SUPPORT_DIR = Path(__file__).parent


class Settings(BaseSettings):
    SECRET_KEY: SecretStr = SecretStr("test-secret-key-for-templating")
    DATABASE_URL: str = "sqlite+aiosqlite://"
    DEBUG: bool = True
    INSTALLED_APPS: list[str] = [
        "obox_builder.contrib.auth",
        "tests.support.apps.shop",
    ]
    TEMPLATE_DIRS: list[Path] = [SUPPORT_DIR / "templates"]
    STATICFILES_DIRS: list[Path] = [SUPPORT_DIR / "staticfiles"]
