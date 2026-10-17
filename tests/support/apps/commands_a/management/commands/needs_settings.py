from __future__ import annotations

from obox_builder.core.management import command

from tests.support import events


@command(help="Record whether settings were configured before running.", requires_settings=True)
def handle() -> None:
    from obox_builder.conf import settings

    events.RECORDS.append(f"needs_settings.configured={settings.configured}")
