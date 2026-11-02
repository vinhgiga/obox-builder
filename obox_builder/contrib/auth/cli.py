"""Interactive user administration commands."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, OperationalError

from obox_builder.conf import load_settings
from obox_builder.contrib.auth.models import User
from obox_builder.db import Database


def add_parser(commands: Any) -> None:
    parser = commands.add_parser("createsuperuser", help="Create a superuser account.")
    parser.add_argument("--username", help="Specifies the login for the superuser.")
    parser.add_argument("--noinput", "--no-input", action="store_false", dest="interactive")


async def _hash_password(password: str, *, validate: bool = True) -> str:
    from obox_builder.contrib.auth.passwords import hash_password

    try:
        return await hash_password(password, validate=validate)
    except ModuleNotFoundError as error:
        if error.name != "pwdlib":
            raise
        raise ValueError("Install obox-builder[auth] to enable password hashing.") from error


def _check_password_support() -> None:
    try:
        from pwdlib import PasswordHash

        PasswordHash.recommended()
    except ModuleNotFoundError as error:
        if error.name != "pwdlib":
            raise
        raise ValueError("Install obox-builder[auth] to enable password hashing.") from error


async def _check_database(database: Database, username: str | None = None) -> None:
    try:
        async with database.lifespan(), database.sessions() as session:
            await session.execute(sa.select(User).limit(1))
            if username is not None:
                exists = await session.scalar(sa.select(User.id).where(User.username == username))
                if exists is not None:
                    raise ValueError(f"A user named {username!r} already exists.")
    except OperationalError as error:
        raise ValueError(
            "Run `uv run alembic upgrade head` before creating a superuser."
        ) from error
    except ModuleNotFoundError as error:
        if error.name != "aiosqlite":
            raise
        raise ValueError(
            "Install obox-builder[sqlite] to use the default SQLite database."
        ) from error


async def _prompt_password_hash() -> str:
    while True:
        password = await asyncio.to_thread(getpass.getpass, "Password: ")
        confirmation = await asyncio.to_thread(getpass.getpass, "Password (again): ")
        if password != confirmation:
            print("Error: Your passwords didn't match.")
            continue
        if not password.strip():
            print("Error: Blank passwords aren't allowed.")
            continue
        if len(password) > 1024:
            print("Use at most 1024 characters for the password.")
            continue
        validate = len(password) >= 12
        if not validate:
            print("Use between 12 and 1024 characters for the password.")
            bypass = await asyncio.to_thread(
                input, "Bypass password validation and create user anyway? [y/N]: "
            )
            if bypass.lower() != "y":
                continue
        return await _hash_password(password, validate=validate)


async def run(arguments: argparse.Namespace) -> None:
    project_root = str(Path.cwd().resolve())
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    settings_module = os.environ.get("OBOX_SETTINGS_MODULE", "setting")
    os.environ["OBOX_SETTINGS_MODULE"] = settings_module
    settings = load_settings(settings_module)
    database = Database(settings)
    _check_password_support()
    await _check_database(database)

    username = arguments.username
    if arguments.interactive:
        if username is None:
            default_username = getpass.getuser()
            entered = await asyncio.to_thread(
                input, f"Username (leave blank to use '{default_username}'): "
            )
            username = entered.strip() or default_username
        else:
            username = username.strip()
    else:
        username = username or os.environ.get("OBOX_SUPERUSER_USERNAME")
        if not username:
            raise ValueError("Use --username or OBOX_SUPERUSER_USERNAME with --noinput.")

    username = username.strip()
    if not username or len(username) > 150:
        raise ValueError("Username must contain between 1 and 150 characters.")
    await _check_database(database, username)

    if arguments.interactive:
        password_hash = await _prompt_password_hash()
    else:
        password = os.environ.get("OBOX_SUPERUSER_PASSWORD")
        if password is None:
            raise ValueError("Set OBOX_SUPERUSER_PASSWORD with --noinput.")
        if not 12 <= len(password) <= 1024:
            raise ValueError("Use between 12 and 1024 characters for the password.")
        password_hash = await _hash_password(password)

    try:
        async with database.lifespan(), database.sessions.begin() as session:
            exists = await session.scalar(sa.select(User.id).where(User.username == username))
            if exists is not None:
                raise ValueError(f"A user named {username!r} already exists.")
            session.add(
                User(
                    username=username,
                    password_hash=password_hash,
                    is_active=True,
                    is_staff=True,
                    is_superuser=True,
                )
            )
    except OperationalError as error:
        raise ValueError(
            "Run `uv run alembic upgrade head` before creating a superuser."
        ) from error
    except ModuleNotFoundError as error:
        if error.name != "aiosqlite":
            raise
        raise ValueError(
            "Install obox-builder[sqlite] to use the default SQLite database."
        ) from error
    except IntegrityError as error:
        raise ValueError(f"A user named {username!r} already exists.") from error

    print(f"Superuser {username!r} created.")
