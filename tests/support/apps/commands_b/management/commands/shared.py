from __future__ import annotations

from obox_builder.core.management import command

from tests.support import events


@command(help="Shared name in the second app.", requires_settings=False)
def handle() -> None:
    events.RECORDS.append("commands_b.shared")
