from __future__ import annotations

from obox_builder.core.management import command

from tests.support import events


@command(help="App override of the core version command.", requires_settings=False)
def handle() -> None:
    events.RECORDS.append("commands_b.version")
