from __future__ import annotations

import getpass
from types import SimpleNamespace
from typing import Any

import typer
from pydantic import TypeAdapter, ValidationError

from obox_builder.core.management import CommandError, utils
from obox_builder.core.management.base import command


@command(help="Create a superuser account.", requires_checks=True)
def handle(
    username: str = typer.Option("", "--username", help="Username (prompted when omitted)."),
    email: str = typer.Option("", "--email", help="Email address."),
    noinput: bool = typer.Option(
        False, "--noinput", help="Read the password from OBOX_SUPERUSER_PASSWORD."
    ),
) -> None:
    import obox_builder
    from obox_builder.conf import settings
    from obox_builder.contrib.auth.models import User
    from obox_builder.contrib.auth.services import (
        create_user,
        get_user_by_username,
        normalize_username,
    )
    from obox_builder.contrib.auth.validators import PasswordValidationError, validate_password
    from obox_builder.db.engine import run_async
    from obox_builder.db.session import session_scope

    async def run() -> None:
        name = username or (email if User.USERNAME_FIELD == User.EMAIL_FIELD else "")
        if not name:
            if noinput:
                raise CommandError("--noinput requires --username.")
            default_name = ""
            if User.USERNAME_FIELD == "username":
                try:
                    default_name = normalize_username(getpass.getuser())
                except (KeyError, OSError):
                    default_name = ""
            async with session_scope() as db:
                if default_name and await get_user_by_username(db, default_name) is not None:
                    default_name = ""
            label = User.USERNAME_FIELD.replace("_", " ").capitalize()
            if default_name:
                label += f" (leave blank to use '{default_name}')"
            name = typer.prompt(label, default=default_name or None, show_default=False)
        address = email
        if (
            User.USERNAME_FIELD != User.EMAIL_FIELD
            and User.EMAIL_FIELD in User.__table__.columns
            and not address
            and not noinput
        ):
            address = typer.prompt("Email address", default="", show_default=False)
        extra_fields: dict[str, Any] = {}
        for field_name in User.REQUIRED_FIELDS:
            if field_name == User.EMAIL_FIELD and address:
                continue
            if noinput:
                raw = settings.SUPERUSER_FIELDS.get(field_name)
                if not raw:
                    raise CommandError(
                        f"--noinput requires {field_name!r} in OBOX_SUPERUSER_FIELDS."
                    )
            else:
                raw = typer.prompt(field_name.replace("_", " ").capitalize())
            try:
                value = TypeAdapter(
                    User.__table__.columns[field_name].type.python_type
                ).validate_python(raw)
            except (KeyError, NotImplementedError, ValidationError) as exc:
                raise CommandError(f"Invalid value for {field_name!r}.") from exc
            if field_name == User.EMAIL_FIELD:
                address = str(value)
            else:
                extra_fields[field_name] = value
        bypass = False
        if noinput:
            secret = settings.SUPERUSER_PASSWORD
            if secret is None:
                raise CommandError("--noinput requires the OBOX_SUPERUSER_PASSWORD variable.")
            password = secret.get_secret_value()
        else:
            while True:
                password = typer.prompt("Password", default="", hide_input=True, show_default=False)
                confirmation = typer.prompt(
                    "Password (again)", default="", hide_input=True, show_default=False
                )
                if password != confirmation:
                    utils.echo("Error: Your passwords didn't match.", err=True)
                    continue
                if not password.strip():
                    utils.echo("Error: Blank passwords aren't allowed.", err=True)
                    continue
                errors = validate_password(
                    password,
                    user=SimpleNamespace(
                        username=normalize_username(name), email=address.strip().lower()
                    ),
                )
                if errors:
                    utils.echo("\n".join(errors), err=True)
                    response = typer.prompt(
                        "Bypass password validation and create user anyway? [y/N]",
                        default="N",
                        show_default=False,
                    )
                    if response.lower() != "y":
                        continue
                    bypass = True
                break
        async with session_scope() as db:
            try:
                user = await create_user(
                    db,
                    name,
                    password,
                    email=address,
                    is_superuser=True,
                    validate=not bypass,
                    **extra_fields,
                )
            except PasswordValidationError as exc:
                raise CommandError(" ".join(exc.errors)) from exc
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
        utils.echo(f"Superuser {user.username!r} created.")

    obox_builder.setup()
    run_async(run())
