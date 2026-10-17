from __future__ import annotations

from obox_builder.apps import apps

from tests.support import events

events.RECORDS.append("payments.models")


class Payment:
    pass


apps.register_model("payments", Payment)
