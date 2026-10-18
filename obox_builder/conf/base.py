from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings as PydanticBaseSettings
from pydantic_settings import SettingsConfigDict

from obox_builder.conf.logging import default_logging_config

DEFAULT_MIDDLEWARE = [
    "obox_builder.core.middleware.request_id.RequestIDMiddleware",
    "obox_builder.core.middleware.security_headers.SecurityHeadersMiddleware",
]


class BaseSettings(PydanticBaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="OBOX_",
        extra="forbid",
    )

    SECRET_KEY: SecretStr
    DEBUG: bool = False
    DATABASE_URL: str
    INSTALLED_APPS: list[str] = Field(default_factory=list)
    MIDDLEWARE: list[str] = Field(default_factory=lambda: list(DEFAULT_MIDDLEWARE))
    ROOT_URLCONF: str = ""
    ASGI_APPLICATION: str = ""
    TEMPLATE_DIRS: list[Path] = Field(default_factory=list)
    TEMPLATE_CONTEXT_PROCESSORS: list[str] = Field(default_factory=list)
    STATIC_URL: str = "/static/"
    STATIC_ROOT: Path | None = None
    STATICFILES_DIRS: list[Path] = Field(default_factory=list)
    LOGIN_URL: str = "/admin/login"
    LOGIN_REDIRECT_URL: str = "/admin/"
    SESSION_COOKIE_NAME: str = "obox_sid"
    SESSION_COOKIE_AGE: int = 12 * 3600
    SESSION_COOKIE_SECURE: bool = True
    SESSION_ABSOLUTE_AGE: int = 7 * 24 * 3600
    LOGIN_MAX_ATTEMPTS: int = 5
    LOGIN_LOCKOUT_SECONDS: int = 900
    PASSWORD_MIN_LENGTH: int = 8
    SUPERUSER_PASSWORD: SecretStr | None = None
    HISTORY_ENABLED: bool = True
    #: ``closed`` fails the transaction when history cannot be written;
    #: ``open`` logs the failure and lets the data change commit.
    HISTORY_FAILURE_MODE: Literal["closed", "open"] = "closed"
    #: What to do about bulk ORM/Core writes against tracked models that bypass
    #: the unit of work: ``ignore``, ``warn`` (log) or ``raise``.
    HISTORY_BULK_POLICY: Literal["ignore", "warn", "raise"] = "warn"
    #: Values larger than this (as compact JSON bytes) are truncated in payloads.
    HISTORY_MAX_VALUE_BYTES: int = 8192
    #: Global retention; ``None`` keeps entries forever.
    HISTORY_RETENTION_DAYS: int | None = None
    #: Proxies whose ``X-Forwarded-For`` header is trusted for client IPs.
    HISTORY_TRUSTED_PROXIES: list[str] = Field(default_factory=list)
    #: Track every model registered on the admin site, even without
    #: ``__history__``.
    HISTORY_TRACK_ALL_ADMIN_MODELS: bool = False
    LOGGING: dict[str, Any] | None = None

    @field_validator("SECRET_KEY")
    @classmethod
    def require_secret_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("SECRET_KEY must not be blank")
        return value

    @field_validator("DATABASE_URL")
    @classmethod
    def require_database_url(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("DATABASE_URL must not be blank")
        return value

    @field_validator("INSTALLED_APPS")
    @classmethod
    def validate_installed_apps(cls, value: list[str]) -> list[str]:
        for entry in value:
            if not entry.strip():
                raise ValueError("INSTALLED_APPS entries must not be blank")
        return value

    @property
    def logging_config(self) -> dict[str, Any]:
        return self.LOGGING or default_logging_config(debug=self.DEBUG)
