from __future__ import annotations

from obox_builder.core.management import command

from tests.support import events


@command(help="Probe command used by the test suite.", requires_settings=False)
def handle() -> None:
    events.RECORDS.append("probe.run")
