"""Optional idempotency application."""

from obox.apps import AppConfig


class IdempotencyConfig(AppConfig):
    name = "obox.contrib.idempotency"

    def ready(self) -> None:
        from functools import partial

        from obox.conf import settings
        from obox.contrib.idempotency import Idempotency

        self._service_factory = partial(Idempotency, **dict(getattr(settings, "IDEMPOTENCY", {})))
