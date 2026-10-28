from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from obox_builder.conf import SETTINGS_MODULE_ENV
from obox_builder.contrib.auth import sessions
from obox_builder.contrib.auth.models import Session as AuthSession
from obox_builder.contrib.auth.models import User
from obox_builder.contrib.auth.permissions import collect_permissions
from obox_builder.core.management.cli import create_typer_app
from obox_builder.db.base import ModelBase
from obox_builder.db.engine import run_async
from obox_builder.db.session import session_scope
from typer.testing import CliRunner


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A file-backed SQLite DB so state survives across engine lifecycles."""
    monkeypatch.setenv(SETTINGS_MODULE_ENV, "tests.support.auth_settings")
    monkeypatch.setenv("OBOX_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite3'}")


async def create_tables() -> None:
    from obox_builder.db import get_engine

    async with get_engine().begin() as connection:
        await connection.run_sync(ModelBase.metadata.create_all)


def test_createsuperuser_with_noinput(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBOX_SUPERUSER_PASSWORD", "s3cure-password")
    run_async(create_tables())
    runner = CliRunner()
    result = runner.invoke(
        create_typer_app(),
        ["createsuperuser", "--username", "Root", "--email", "root@example.com", "--noinput"],
    )
    assert result.exit_code == 0, result.output
    assert "Superuser 'root' created." in result.output

    async def check() -> None:
        async with session_scope() as db:
            user = await db.scalar(sa.select(User).where(User.username == "root"))
            assert user is not None
            assert user.is_superuser
            assert user.email == "root@example.com"

    run_async(check())


def test_createsuperuser_noinput_requires_password(env: None) -> None:
    runner = CliRunner()
    result = runner.invoke(
        create_typer_app(), ["createsuperuser", "--username", "root", "--noinput"]
    )
    assert result.exit_code == 1
    assert "OBOX_SUPERUSER_PASSWORD" in result.output


def test_createsuperuser_interactive(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("getpass.getuser", lambda: "Root")
    run_async(create_tables())
    runner = CliRunner()
    result = runner.invoke(
        create_typer_app(),
        ["createsuperuser"],
        input="\n\ns3cure-password\ns3cure-password\n",
    )
    assert result.exit_code == 0, result.output
    assert "Superuser 'root' created." in result.output
    assert "Username (leave blank to use 'root')" in result.output


@pytest.mark.parametrize(
    ("entered", "password", "message"),
    [
        ("123456\n123456\ny\n", "123456", "Bypass password validation"),
        ("123456\n123456\nY\n", "123456", "Bypass password validation"),
        (
            "123456\n123456\nn\ns3cure-password\ns3cure-password\n",
            "s3cure-password",
            "Bypass password validation",
        ),
        (
            "123456\n123456\n\ns3cure-password\ns3cure-password\n",
            "s3cure-password",
            "Bypass password validation",
        ),
        (
            "123456\n123456\nyes\ns3cure-password\ns3cure-password\n",
            "s3cure-password",
            "Bypass password validation",
        ),
        ("\n\ns3cure-password\ns3cure-password\n", "s3cure-password", "Blank passwords"),
        ("   \n   \ns3cure-password\ns3cure-password\n", "s3cure-password", "Blank passwords"),
        (
            "first\nsecond\ns3cure-password\ns3cure-password\n",
            "s3cure-password",
            "Your passwords didn't match",
        ),
    ],
)
def test_createsuperuser_password_prompt(
    env: None, entered: str, password: str, message: str
) -> None:
    from obox_builder.contrib.auth.services import authenticate

    run_async(create_tables())
    result = CliRunner().invoke(
        create_typer_app(),
        ["createsuperuser", "--username", "Root", "--email", "root@example.com"],
        input=entered,
    )
    assert result.exit_code == 0, result.output
    assert message in result.output
    assert password not in result.output

    async def check() -> None:
        async with session_scope() as db:
            user = await authenticate(db, "root", password)
            assert user is not None
            assert user.is_superuser
            assert user.email == "root@example.com"

    run_async(check())


@pytest.mark.parametrize("noinput", [False, True])
def test_createsuperuser_does_not_save_an_unconfirmed_weak_password(
    env: None, monkeypatch: pytest.MonkeyPatch, noinput: bool
) -> None:
    monkeypatch.setenv("OBOX_SUPERUSER_PASSWORD", "123456")
    run_async(create_tables())
    args = ["createsuperuser", "--username", "root", "--email", "root@example.com"]
    if noinput:
        args.append("--noinput")
    result = CliRunner().invoke(create_typer_app(), args, input="123456\n123456\n")
    assert result.exit_code == 1, result.output
    assert ("Bypass password validation" in result.output) is not noinput

    async def check() -> None:
        async with session_scope() as db:
            assert await db.scalar(sa.select(User)) is None

    run_async(check())


def test_createsuperuser_rejects_duplicates(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBOX_SUPERUSER_PASSWORD", "s3cure-password")
    run_async(create_tables())
    runner = CliRunner()
    first = runner.invoke(
        create_typer_app(), ["createsuperuser", "--username", "root", "--noinput"]
    )
    assert first.exit_code == 0, first.output
    second = runner.invoke(
        create_typer_app(), ["createsuperuser", "--username", "root", "--noinput"]
    )
    assert second.exit_code == 1
    assert "already exists" in second.output


def test_changepassword_revokes_sessions(env: None) -> None:
    async def seed() -> None:
        from obox_builder.contrib.auth.services import create_user

        await create_tables()
        async with session_scope() as db:
            user = await create_user(db, "alice", "old-s3cure-password")
            await sessions.create_session(db, user.id)
            await sessions.create_session(db, user.id)

    run_async(seed())

    runner = CliRunner()
    result = runner.invoke(
        create_typer_app(),
        ["changepassword", "alice"],
        input="new-s3cure-password\nnew-s3cure-password\n",
    )
    assert result.exit_code == 0, result.output
    assert "2 session(s) revoked" in result.output

    async def check() -> None:
        from obox_builder.contrib.auth.services import authenticate

        async with session_scope() as db:
            assert await db.scalar(sa.select(AuthSession)) is None
            assert await authenticate(db, "alice", "new-s3cure-password") is not None
            assert await authenticate(db, "alice", "old-s3cure-password") is None

    run_async(check())


def test_changepassword_unknown_user(env: None) -> None:
    run_async(create_tables())
    runner = CliRunner()
    result = runner.invoke(
        create_typer_app(),
        ["changepassword", "nobody"],
        input="s3cure-password\ns3cure-password\n",
    )
    assert result.exit_code == 1
    assert "does not exist" in result.output


def test_sync_permissions_command(env: None) -> None:
    run_async(create_tables())
    runner = CliRunner()
    result = runner.invoke(create_typer_app(), ["sync_permissions"])
    assert result.exit_code == 0, result.output
    assert f"Created {len(collect_permissions())} permission(s)." in result.output

    again = runner.invoke(create_typer_app(), ["sync_permissions", "--dry-run"])
    assert again.exit_code == 0, again.output
    assert "Would create 0 permission(s)." in again.output


def test_post_migrate_signal_syncs_permissions(env: None) -> None:
    async def emit_and_check() -> None:
        import obox_builder
        from obox_builder.contrib.auth.models import Permission
        from obox_builder.contrib.auth.permissions import collect_permissions
        from obox_builder.core.signals import post_migrate

        obox_builder.setup()
        await create_tables()
        await post_migrate.send_async(sender="migrate")
        async with session_scope() as db:
            rows = (await db.scalars(sa.select(Permission))).all()
            assert {row.full_name for row in rows} == {
                entry.full_name for entry in collect_permissions()
            }

    run_async(emit_and_check())
