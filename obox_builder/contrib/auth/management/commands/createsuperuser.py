from __future__ import annotations

import typer

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
    from obox_builder.contrib.auth.services import create_user
    from obox_builder.contrib.auth.validators import PasswordValidationError
    from obox_builder.db.engine import run_async
    from obox_builder.db.session import session_scope

    async def run() -> None:
        name = username
        if not name:
            if noinput:
                raise CommandError("--noinput requires --username.")
            name = typer.prompt("Username")
        address = email
        if not address and not noinput:
            address = typer.prompt("Email", default="", show_default=False)
        if noinput:
            secret = settings.SUPERUSER_PASSWORD
            if secret is None:
                raise CommandError("--noinput requires the OBOX_SUPERUSER_PASSWORD variable.")
            password = secret.get_secret_value()
        else:
            password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
        async with session_scope() as db:
            try:
                user = await create_user(db, name, password, email=address, is_superuser=True)
            except PasswordValidationError as exc:
                raise CommandError(" ".join(exc.errors)) from exc
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
        utils.echo(f"Superuser {user.username!r} created.")

    obox_builder.setup()
    run_async(run())
