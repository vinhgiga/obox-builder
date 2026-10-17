from __future__ import annotations

from obox_builder.core.management import command

from tests.support import events


@command(help="Shared name to test override precedence.", requires_settings=False)
def handle() -> None:
    events.RECORDS.append("commands_a.shared")
