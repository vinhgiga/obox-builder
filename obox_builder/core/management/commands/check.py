from __future__ import annotations

import typer

from obox_builder.core.checks import run_checks
from obox_builder.core.management import utils
from obox_builder.core.management.base import CommandError, command


@command(help="Check the project for configuration problems.")
def handle(
    deploy: bool = typer.Option(False, "--deploy", help="Include deployment checks."),
) -> None:
    messages = run_checks(deploy=deploy)
    errors = []
    for message in messages:
        utils.echo(utils.format_check_message(message), level=0)
        if message.is_serious:
            errors.append(message)
    if errors:
        raise CommandError(f"System check identified {len(errors)} error(s).")
    utils.echo("System check identified no issues.")
