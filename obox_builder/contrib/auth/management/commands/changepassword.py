from __future__ import annotations

import typer

from obox_builder.core.management import CommandError, utils
from obox_builder.core.management.base import command


@command(help="Change a user's password and revoke their sessions.", requires_checks=True)
def handle(
    username: str = typer.Argument(..., help="Username to change the password for."),
) -> None:
    import obox_builder
    from obox_builder.contrib.auth.services import get_user_by_username, set_password
    from obox_builder.contrib.auth.validators import PasswordValidationError
    from obox_builder.db.engine import run_async
    from obox_builder.db.session import session_scope

    async def run() -> None:
        password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
        async with session_scope() as db:
            user = await get_user_by_username(db, username)
            if user is None:
                raise CommandError(f"User {username!r} does not exist.")
            try:
                revoked = await set_password(db, user, password)
            except PasswordValidationError as exc:
                raise CommandError(" ".join(exc.errors)) from exc
        utils.echo(f"Password changed for {user.username!r}; {revoked} session(s) revoked.")

    obox_builder.setup()
    run_async(run())
