from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_custom_auth_user_is_loaded_before_dependent_models() -> None:
    project_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["OBOX_SETTINGS_MODULE"] = "tests.support.custom_auth_settings"
    script = """
from sqlalchemy.orm import configure_mappers
from obox_builder import setup
setup()

from obox_builder.apps import apps
from obox_builder.contrib.auth.models import (
    ApiToken,
    Permission,
    Role,
    Session,
    USER_VIEW_PERMISSION,
    User,
)
from obox_builder.contrib.admin.navigation import default_navigation
from obox_builder.db.base import ModelBase

custom_user = apps.get_model("customusers", "Account")
assert User is custom_user
assert User.__tablename__ == "customusers_account"
assert {model.__tablename__ for model in (Role, Permission, Session, ApiToken)} == {
    "auth_role", "auth_permission", "auth_session", "auth_api_token"
}
assert USER_VIEW_PERMISSION == "customusers.view_account"
user_item = next(
    item for group in default_navigation() for item in group.items if item.url == "/admin/users"
)
assert user_item.required_perm == USER_VIEW_PERMISSION
configure_mappers()
for table_name in (
    "auth_session",
    "auth_api_token",
    "auth_password_reset_token",
    "auth_user_roles",
    "obox_saved_view",
    "obox_row_comment",
    "obox_comment_mention",
):
    table = ModelBase.metadata.tables[table_name]
    assert any(
        fk.target_fullname == "customusers_account.id"
        for column in table.columns
        for fk in column.foreign_keys
    ), table_name
"""
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        check=False,
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_email_user_authentication_and_admin() -> None:
    project_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["OBOX_SETTINGS_MODULE"] = "tests.support.email_auth_settings"
    script = """
import asyncio
import httpx
import obox_builder
obox_builder.setup()
from obox_builder.contrib.admin import AdminSite, ModelAdmin
from obox_builder.contrib.auth import password_reset, services
from obox_builder.contrib.auth import get_user_model
from obox_builder.conf import settings
from obox_builder.contrib.auth.models import User
from obox_builder.core.application import create_app
from obox_builder.db import get_engine
from obox_builder.db.base import ModelBase
from obox_builder.db.session import session_scope

async def main():
    async with get_engine().begin() as conn:
        await conn.run_sync(ModelBase.metadata.create_all)
    async with session_scope() as db:
        assert get_user_model() is User
        user = await services.create_user(
            db, "ALICE@EXAMPLE.ORG", "s3cure-password",
            is_superuser=True, department="Engineering",
        )
        assert user.email == "alice@example.org"
        assert user.department == "Engineering"
        assert user.username == "alice@example.org"
        second = await services.create_user(
            db, email="bob@example.org", password="s3cure-password", department="Sales"
        )
        assert second.email == "bob@example.org"
        found = await services.authenticate(db, "alice@example.org", "s3cure-password")
        assert found is not None and found.id == user.id
        assert await services.authenticate(
            db, email="alice@example.org", password="s3cure-password"
        )
        assert await services.authenticate(db, "alice@example.org", "wrong") is None
    site = AdminSite("custom")
    site.api_auth_enabled = True
    class UserAdmin(ModelAdmin):
        api_enabled = True
    site.register(User, UserAdmin)
    app = create_app()
    app.include_router(site.get_router(), prefix="/admin")
    app.include_router(site.get_api_router(), prefix="/api/admin/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        credentials = {"username": "alice@example.org", "password": "s3cure-password"}
        response = await client.post("/admin/login", data=credentials)
        assert response.status_code == 303, response.text
        response = await client.get("/admin/")
        assert response.status_code == 200, response.text
        response = await client.get("/admin/users")
        assert response.status_code == 200, response.text
        response = await client.post("/api/admin/v1/auth/login", json=credentials)
        assert response.status_code == 200, response.text
        response = await client.get("/api/admin/v1/auth/me")
        assert response.status_code == 200, response.text
        assert response.json()["user"]["username"] == "alice@example.org"
        csrf_token = response.json()["csrf_token"]
        response = await client.get("/api/admin/v1/users")
        assert response.status_code == 200, response.text
        assert response.json()["users"][0]["email"] == "alice@example.org"
        response = await client.get("/api/admin/v1/models/emailusers.account/schema")
        assert response.status_code == 404, response.text
        response = await client.post(
            "/api/admin/v1/tokens", headers={"X-CSRF-Token": csrf_token},
            json={"name": "custom", "scopes": ["emailusers.account:read"]},
        )
        assert response.status_code == 201, response.text
        token = response.json()["token"]
        response = await client.get(
            "/api/admin/v1/users", headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        long_email = "z" * 151 + "@example.org"
        async with session_scope() as db:
            await services.create_user(db, email=long_email, password="s3cure-password")
        response = await client.post("/api/admin/v1/auth/login", json={
            "username": long_email, "password": "s3cure-password",
        })
        assert response.status_code == 200, response.text
    from tests.support import email_backend
    from urllib.parse import parse_qs, urlsplit
    settings.configure(settings.load().model_copy(update={
        "EMAIL_BACKEND": "tests.support.email_backend.MemoryBackend",
        "PASSWORD_RESET_COOLDOWN": 0,
    }))
    email_backend.MESSAGES.clear()
    async with session_scope() as db:
        await password_reset.request_password_reset(db, "ALICE@EXAMPLE.ORG")
        assert len(email_backend.MESSAGES) == 1
        link = email_backend.MESSAGES[0].body.splitlines()[2]
        token = parse_qs(urlsplit(link).query)["token"][0]
        assert await password_reset.reset_password(db, token, "brand-new-secret")
        assert await services.authenticate(db, "alice@example.org", "brand-new-secret") is not None
asyncio.run(main())
"""
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        check=False,
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_custom_user_superuser_required_fields(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment.update(
        OBOX_SETTINGS_MODULE="tests.support.email_auth_settings",
        OBOX_DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'custom.sqlite3'}",
        OBOX_SUPERUSER_PASSWORD="s3cure-password",
        OBOX_SUPERUSER_FIELDS='{"department":"Engineering"}',
    )
    script = """
import obox_builder
obox_builder.setup()
from obox_builder.conf import settings
from obox_builder.core.management.cli import create_typer_app
from obox_builder.db import get_engine
from obox_builder.db.base import ModelBase
from obox_builder.db.engine import run_async
from obox_builder.db.session import session_scope
from obox_builder.contrib.auth.models import User
from sqlalchemy import select
from typer.testing import CliRunner

async def setup():
    async with get_engine().begin() as conn:
        await conn.run_sync(ModelBase.metadata.create_all)
run_async(setup())
runner = CliRunner()
args = ["createsuperuser", "--email", "root@example.org", "--noinput"]
settings._wrapped.SUPERUSER_FIELDS = {}
missing = runner.invoke(create_typer_app(), args)
assert missing.exit_code == 1 and "OBOX_SUPERUSER_FIELDS" in missing.output, missing.output
settings._wrapped.SUPERUSER_FIELDS = {"department": "Engineering"}
created = runner.invoke(create_typer_app(), args)
assert created.exit_code == 0, created.output
async def check():
    async with session_scope() as db:
        user = await db.scalar(select(User))
        assert user.email == "root@example.org" and user.department == "Engineering"
        assert user.is_superuser
run_async(check())
"""
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        check=False,
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    ("model", "message"),
    [
        ("NonUniqueAccount", "USERNAME_FIELD must be unique"),
        ("PartialAccount", "USERNAME_FIELD must be unique"),
        ("MissingIdentifierAccount", "invalid USERNAME_FIELD"),
        ("InvalidRequiredAccount", "invalid REQUIRED_FIELDS"),
    ],
)
def test_invalid_custom_user_configuration(model: str, message: str) -> None:
    environment = os.environ.copy()
    environment["OBOX_SETTINGS_MODULE"] = "tests.support.email_auth_settings"
    environment["OBOX_AUTH_USER_MODEL"] = f"emailusers.{model}"
    environment["TEST_INVALID_USER_MODEL"] = model
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", "import obox_builder; obox_builder.setup()"],
        check=False,
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert message in completed.stderr


def test_unique_index_is_accepted_for_identifier() -> None:
    environment = os.environ.copy()
    environment.update(
        OBOX_SETTINGS_MODULE="tests.support.email_auth_settings",
        OBOX_AUTH_USER_MODEL="emailusers.IndexAccount",
        TEST_INVALID_USER_MODEL="IndexAccount",
    )
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", "import obox_builder; obox_builder.setup()"],
        check=False,
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_numeric_identifier_authentication_and_search() -> None:
    environment = os.environ.copy()
    environment.update(
        OBOX_SETTINGS_MODULE="tests.support.email_auth_settings",
        OBOX_AUTH_USER_MODEL="emailusers.NumericAccount",
        TEST_INVALID_USER_MODEL="NumericAccount",
    )
    script = """
import asyncio
import httpx
import obox_builder
obox_builder.setup()
from obox_builder.contrib.admin import AdminSite
from obox_builder.contrib.auth import services
from obox_builder.contrib.auth.models import User
from obox_builder.core.application import create_app
from obox_builder.db import get_engine
from obox_builder.db.base import ModelBase
from obox_builder.db.session import session_scope
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

async def main():
    query = select(User.username).where(User.username.icontains("123"))
    assert "CAST(" in str(query.compile(dialect=postgresql.dialect()))
    async with get_engine().begin() as conn:
        await conn.run_sync(ModelBase.metadata.create_all)
    async with session_scope() as db:
        user = await services.create_user(db, password="s3cure-password", employee_number=123,
                                          is_superuser=True)
        assert user.employee_number == 123 and user.username == "123"
        assert await services.authenticate(db, "123", "s3cure-password") is not None
        assert await services.authenticate(db, employee_number=123, password="s3cure-password")
        assert await services.authenticate(db, "not-a-number", "s3cure-password") is None
    site = AdminSite("custom")
    app = create_app()
    app.include_router(site.get_router(), prefix="/admin")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/admin/login", data={
            "username": "123", "password": "s3cure-password",
        })
        assert response.status_code == 303, response.text
        response = await client.get("/admin/users?q=123")
        assert response.status_code == 200, response.text
        assert "123" in response.text
asyncio.run(main())
"""
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        check=False,
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_custom_email_field() -> None:
    environment = os.environ.copy()
    environment.update(
        OBOX_SETTINGS_MODULE="tests.support.email_auth_settings",
        OBOX_AUTH_USER_MODEL="emailusers.AliasAccount",
        TEST_INVALID_USER_MODEL="AliasAccount",
    )
    script = """
import asyncio
import httpx
import obox_builder
obox_builder.setup()
from obox_builder.contrib.admin import AdminSite
from obox_builder.conf import settings
from obox_builder.contrib.auth import password_reset, services
from obox_builder.core.application import create_app
from obox_builder.db import get_engine
from obox_builder.db.base import ModelBase
from obox_builder.db.session import session_scope
from tests.support import email_backend

async def main():
    async with get_engine().begin() as conn:
        await conn.run_sync(ModelBase.metadata.create_all)
    settings.configure(settings.load().model_copy(update={
        "EMAIL_BACKEND": "tests.support.email_backend.MemoryBackend",
    }))
    async with session_scope() as db:
        user = await services.create_user(
            db, email="Alice@example.org", password="s3cure-password", is_superuser=True
        )
        assert user.contact == "alice@example.org"
        user_id = user.id
        assert await services.authenticate(db, "alice@example.org", "s3cure-password") is not None
        await password_reset.request_password_reset(db, "alice@example.org")
        assert len(email_backend.MESSAGES) == 1
        assert email_backend.MESSAGES[0].recipients == ("alice@example.org",)
    site = AdminSite("custom")
    app = create_app()
    app.include_router(site.get_router(), prefix="/admin")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/admin/login", data={
            "username": "alice@example.org", "password": "s3cure-password",
        })
        assert response.status_code == 303, response.text
        response = await client.get(f"/admin/users/{user_id}")
        assert response.status_code == 200, response.text
        assert "alice@example.org" in response.text
asyncio.run(main())
"""
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        check=False,
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
