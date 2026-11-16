"""Optional activity application."""

from obox.apps import AppConfig


class ActivityConfig(AppConfig):
    name = "obox.contrib.activity"

    def ready(self) -> None:
        from functools import partial

        from obox.conf import settings
        from obox.contrib.activity import Activity

        self._service_factory = partial(Activity, **dict(getattr(settings, "ACTIVITY", {})))
