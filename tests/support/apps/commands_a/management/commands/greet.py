from __future__ import annotations

import typer
from obox_builder.core.management import command

from tests.support import events


@command(help="Record greeting arguments.", requires_settings=False)
def handle(
    name: str = typer.Argument("world"),
    count: int = typer.Option(1, "--count", "-n"),
) -> None:
    events.RECORDS.append(f"greet:{name}:{count}")
