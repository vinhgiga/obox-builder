from __future__ import annotations

import pytest
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.db.engine import to_sync_url


def test_to_sync_url_translates_async_drivers() -> None:
    assert (
        to_sync_url("postgresql+psycopg_async://user:pw@host:5432/db")
        == "postgresql+psycopg://user:pw@host:5432/db"
    )
    assert (
        to_sync_url("postgresql+asyncpg://user:pw@host/db")
        == "postgresql+psycopg://user:pw@host/db"
    )
    assert to_sync_url("sqlite+aiosqlite:///tmp/db.sqlite3") == "sqlite:///tmp/db.sqlite3"


def test_to_sync_url_passes_through_sync_drivers() -> None:
    assert to_sync_url("postgresql+psycopg://h/db") == "postgresql+psycopg://h/db"
    assert to_sync_url("sqlite:///tmp/db.sqlite3") == "sqlite:///tmp/db.sqlite3"


def test_to_sync_url_rejects_unknown_drivers() -> None:
    with pytest.raises(ImproperlyConfigured, match="Unsupported DATABASE_URL driver"):
        to_sync_url("mysql+aiomysql://h/db")
