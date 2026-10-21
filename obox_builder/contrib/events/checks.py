from __future__ import annotations

import sqlalchemy as sa

from obox_builder.apps import apps
from obox_builder.conf import settings
from obox_builder.contrib.events.options import parse_events
from obox_builder.core.checks import register
from obox_builder.core.checks.messages import CheckMessage, Error, Warning
from obox_builder.core.events import subscriptions
from obox_builder.core.exceptions import ImproperlyConfigured


@register(tags=("events",))
def check_events() -> list[CheckMessage]:
    if not apps.models_ready or not apps.is_installed("obox_builder.contrib.events"):
        return []
    messages: list[CheckMessage] = []
    for model in apps.get_models():
        label = f"{model._model_label}.{model.__name__}"
        try:
            options = parse_events(model).history
        except (ImproperlyConfigured, TypeError, ValueError) as exc:
            messages.append(Error(f"{label}: {exc}", id="obox.events.E401"))
            continue
        if not options.enabled:
            continue
        mapper = sa.inspect(model)
        if len(mapper.primary_key) != 1:
            messages.append(
                Error(f"{label}: events require a single-column PK.", id="obox.events.E402")
            )
        fields = set(options.fields or ()) | options.exclude | options.mask
        for name in sorted(fields - set(mapper.attrs.keys())):
            messages.append(Error(f"{label}: unknown event field {name!r}.", id="obox.events.E403"))
        for name in options.relations:
            relation = mapper.relationships.get(name)
            if relation is None or relation.secondary is None:
                messages.append(
                    Error(f"{label}: event relation {name!r} must be M2M.", id="obox.events.E404")
                )
    for subscription in subscriptions():
        if subscription.timeout >= settings.EVENTS_LEASE_SECONDS:
            messages.append(
                Error(
                    f"{subscription.name}: timeout must be shorter than EVENTS_LEASE_SECONDS.",
                    id="obox.events.E405",
                )
            )
    if settings.EVENTS_BULK_POLICY != "raise":
        messages.append(
            Warning(
                "Bulk ORM DML and raw SQL bypass event capture; "
                "use per-object writes or emit explicitly.",
                id="obox.events.W401",
            )
        )
    return messages
