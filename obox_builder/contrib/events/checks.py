from __future__ import annotations

import sqlalchemy as sa

from obox_builder.apps import apps
from obox_builder.conf import settings
from obox_builder.contrib.events.options import parse_events
from obox_builder.core.checks import register
from obox_builder.core.checks.messages import CheckMessage, Error, Warning
from obox_builder.core.events import subscriptions
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.db.router import router


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


@register(tags=("config",))
def check_event_database() -> list[CheckMessage]:
    if not (
        settings.EVENTS_ENABLED
        and apps.models_ready
        and apps.is_installed("obox_builder.contrib.events")
    ):
        return []

    from obox_builder.contrib.events.models import Delivery, Event

    event_database = "default"
    messages: list[CheckMessage] = []
    if any(router.db_for_write(model) != event_database for model in (Event, Delivery)):
        messages.append(
            Error(
                "Event and Delivery tables must use the default database because event capture "
                "writes through the session's default connection.",
                id="obox.E007",
            )
        )
    for model in apps.get_models():
        if (
            getattr(model, "__events__", None) not in (None, False)
            and router.db_for_write(model) != event_database
        ):
            messages.append(
                Error(
                    f"Event capture for {model._model_label}.{model.__name__} requires the "
                    "default database.",
                    hint="Route event-enabled models to default so their outbox rows commit "
                    "with the model changes.",
                    id="obox.E007",
                )
            )
    return messages
