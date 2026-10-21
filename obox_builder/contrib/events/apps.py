from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from obox_builder.apps import AppConfig, apps
from obox_builder.conf import settings


class EventsConfig(AppConfig):
    name = "obox_builder.contrib.events"
    label = "events"
    verbose_name = "Events"

    def ready(self) -> None:
        from obox_builder.contrib.events import checks  # noqa: F401
        from obox_builder.contrib.events.capture import ensure_listeners
        from obox_builder.contrib.events.options import _trackers
        from obox_builder.utils.module_loading import autodiscover_modules

        _trackers.clear()
        ensure_listeners()
        autodiscover_modules("events", packages=[config.name for config in apps.get_app_configs()])

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        if not (settings.EVENTS_ENABLED and settings.EVENTS_EMBEDDED_WORKER):
            yield
            return
        from obox_builder.contrib.events.worker import Worker, shutdown
        from obox_builder.core.events import subscriptions

        queues = tuple(sorted({s.queue for s in subscriptions()})) or ("default",)
        worker = Worker(queues=queues)
        task = asyncio.create_task(worker.run(), name="obox.events.worker")
        try:
            yield
        finally:
            await shutdown(worker, task)
