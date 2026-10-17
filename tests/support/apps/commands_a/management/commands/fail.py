from __future__ import annotations

from obox_builder.core.management import CommandError, command


@command(help="Always fails with a CommandError.", requires_settings=False)
def handle() -> None:
    raise CommandError("boom")
