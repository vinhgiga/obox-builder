from __future__ import annotations

from tests.support.custom_auth_settings import Settings as BaseSettings


class Settings(BaseSettings):
    INSTALLED_APPS: list[str] = [
        "obox_builder.contrib.auth",
        "obox_builder.contrib.admin",
        "obox_builder.contrib.comments",
        "tests.support.apps.emailusers",
    ]
    AUTH_USER_MODEL: str = "emailusers.Account"
    DEBUG: bool = True
    SESSION_COOKIE_SECURE: bool = False
