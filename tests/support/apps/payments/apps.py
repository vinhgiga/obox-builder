from __future__ import annotations

from obox_builder.apps import AppConfig

from tests.support import events


class PaymentsConfig(AppConfig):
    def ready(self) -> None:
        events.RECORDS.append("payments.ready")
