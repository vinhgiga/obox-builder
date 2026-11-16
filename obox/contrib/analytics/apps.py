"""Optional analytics application."""

from obox.apps import AppConfig


class AnalyticsConfig(AppConfig):
    name = "obox.contrib.analytics"

    def ready(self) -> None:
        from functools import partial

        from obox.conf import settings
        from obox.contrib.analytics import Analytics

        self._service_factory = partial(Analytics, **dict(getattr(settings, "ANALYTICS", {})))
