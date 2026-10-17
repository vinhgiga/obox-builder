from __future__ import annotations

from obox_builder.conf import BaseSettings


class Settings(BaseSettings):
    INSTALLED_APPS: list[str] = []
    MIDDLEWARE: list[str] = []
    ROOT_URLCONF: str = "config.urls"
