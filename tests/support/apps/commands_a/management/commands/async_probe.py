from __future__ import annotations

import asyncio

from obox_builder.core.management import command

from tests.support import events


@command(help="Async probe command.", requires_settings=False)
async def handle() -> None:
    await asyncio.sleep(0)
    events.RECORDS.append("async_probe.run")
