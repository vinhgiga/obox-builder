from __future__ import annotations

from obox_builder.conf import BaseSettings
from pydantic import SecretStr


class Settings(BaseSettings):
    SECRET_KEY: SecretStr = SecretStr("x" * 48)
    DATABASE_URL: str = "sqlite+aiosqlite://"
    INSTALLED_APPS: list[str] = [
        "tests.support.apps.commands_a",
        "tests.support.apps.commands_b",
        "tests.support.apps.blog",
    ]
    ROOT_URLCONF: str = "tests.support.urls"
    ASGI_APPLICATION: str = "tests.support.asgi.application"
