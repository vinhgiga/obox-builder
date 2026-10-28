from __future__ import annotations

from pathlib import Path

from obox_builder.conf import BaseSettings
from pydantic import SecretStr


class Settings(BaseSettings):
    SECRET_KEY: SecretStr = SecretStr("auth-settings-secret-key-0123456789abcdef")
    DATABASE_URL: str = "sqlite+aiosqlite:///:memory:"
    INSTALLED_APPS: list[str] = [
        "obox_builder.contrib.auth",
        "tests.support.apps.shop",
    ]
    LOGIN_URL: str = "/admin/login"
    LOGIN_REDIRECT_URL: str = "/admin/"
    SESSION_COOKIE_NAME: str = "obox_sid"
    SESSION_COOKIE_SECURE: bool = False
    PASSWORD_MIN_LENGTH: int = 8
    ROOT_URLCONF: str = ""
    TEMPLATE_DIRS: list[Path] = []
