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
    # -- row comments -------------------------------------------------------
    #: Master switch for ``__comments__`` models and their UI.
    COMMENTS_ENABLED: bool = True
    #: Application cap per comment (the database hard cap is 20,000).
    COMMENTS_MAX_LENGTH: int = Field(default=10_000, ge=1, le=20_000)
    #: Maximum number of distinct users mentionable in one comment.
    COMMENTS_MAX_MENTIONS: int = Field(default=20, ge=1, le=100)
    #: Authors may edit their own comment within this window; ``None`` is unlimited.
    COMMENTS_EDIT_WINDOW_SECONDS: int | None = Field(default=None, ge=0)
    #: Review window before deleted comments are purged (Phase 2).
    COMMENTS_DELETED_RETENTION_DAYS: int = Field(default=30, ge=0)
    #: Read notifications are removed after this many days (Phase 2).
    COMMENTS_NOTIFICATION_RETENTION_DAYS: int = Field(default=90, ge=0)
    #: Comments per author per minute; ``0`` disables the limiter.
    COMMENTS_RATE_LIMIT_PER_MINUTE: int = Field(default=20, ge=0)
    #: Implicit subscription mode after commenting or being mentioned (Phase 2).
    COMMENTS_AUTO_SUBSCRIBE_MODE: Literal["all", "mentions", "muted"] = "all"
    #: Include the comment body in outgoing email (Phase 3; privacy default off).
    COMMENTS_NOTIFY_EMAIL_INCLUDE_BODY: bool = False
    #: Outbox attempts before a delivery is parked (Phase 3).
    COMMENTS_OUTBOX_MAX_ATTEMPTS: int = Field(default=8, ge=1)
    # -- durable events -----------------------------------------------------
    EVENTS_ENABLED: bool = True
    EVENTS_EMBEDDED_WORKER: bool = False
    EVENTS_POLL_INTERVAL: float = Field(default=1.0, gt=0)
    EVENTS_LEASE_SECONDS: int = Field(default=120, ge=2)
    EVENTS_SHUTDOWN_TIMEOUT: float = Field(default=30.0, gt=0)
    EVENTS_MAX_PAYLOAD_BYTES: int = Field(default=262_144, ge=1)
    EVENTS_MAX_HOPS: int = Field(default=16, ge=1, le=100)
    EVENTS_BULK_POLICY: Literal["ignore", "warn", "raise"] = "warn"
    EVENTS_RETRY_BASE_SECONDS: float = Field(default=2.0, gt=0)
    EVENTS_RETRY_MAX_SECONDS: float = Field(default=3600.0, gt=0)
    # -- process-isolated background jobs ----------------------------------
    JOBS_ENABLED: bool = True
    JOBS_POLL_INTERVAL: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    JOBS_LEASE_SECONDS: int = Field(default=60, ge=3)
    JOBS_WORKER_HEARTBEAT_SECONDS: float = Field(default=5.0, ge=0.1, allow_inf_nan=False)
    JOBS_SHUTDOWN_TIMEOUT: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    JOBS_MAX_PAYLOAD_BYTES: int = Field(default=262_144, ge=1)
    JOBS_RETRY_BACKOFF: float = Field(default=2.0, gt=0, allow_inf_nan=False)
    JOBS_RETRY_BACKOFF_MAX: float = Field(default=600.0, gt=0, allow_inf_nan=False)
    IMPORT_MAX_FILE_BYTES: int = Field(default=50 * 1024 * 1024, ge=1)
    IMPORT_MAX_UNPACKED_BYTES: int = Field(default=512 * 1024 * 1024, ge=1)
    IMPORT_MAX_ROWS: int = Field(default=100_000, ge=1, le=100_000)
    IMPORT_MAX_COLUMNS: int = Field(default=200, ge=1, le=1000)
    IMPORT_RETENTION_DAYS: int = Field(default=7, ge=1)
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
