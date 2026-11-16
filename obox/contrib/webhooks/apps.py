"""Optional webhooks application."""

from obox.apps import AppConfig


class WebhooksConfig(AppConfig):
    name = "obox.contrib.webhooks"

    def ready(self) -> None:
        from functools import partial

        from obox.conf import settings
        from obox.contrib.webhooks import Webhooks

        self._service_factory = partial(Webhooks, **dict(getattr(settings, "WEBHOOKS", {})))
