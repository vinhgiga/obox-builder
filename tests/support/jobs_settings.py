from __future__ import annotations

from obox_builder.conf import BaseSettings
from pydantic import SecretStr


class Settings(BaseSettings):
    SECRET_KEY: SecretStr = SecretStr("jobs-verification-key-at-least-32-characters")
    DATABASE_URL: str = "sqlite+aiosqlite:///:memory:"
    INSTALLED_APPS: list[str] = ["obox_builder.contrib.jobs", "tests.support.apps.jobsdemo"]
    JOBS_LEASE_SECONDS: int = 3
    JOBS_POLL_INTERVAL: float = 0.05
    JOBS_RETRY_BACKOFF: float = 0.01
    JOBS_RETRY_BACKOFF_MAX: float = 0.01
