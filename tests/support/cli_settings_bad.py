from __future__ import annotations

from obox_builder.conf import BaseSettings
from pydantic import SecretStr


class Settings(BaseSettings):
    SECRET_KEY: SecretStr = SecretStr("changeme")
    DEBUG: bool = True
    DATABASE_URL: str = "sqlite+aiosqlite://"
    ASGI_APPLICATION: str = "tests.support.asgi.application"
