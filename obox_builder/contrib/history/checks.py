"""System checks for declared ``__history__`` models.

* ``obox.history.E201`` — tracked model has a composite primary key (v1 does
  not support it).
* ``obox.history.E202`` — ``relations`` entry is not a many-to-many
  relationship (only M2M is recordable).
* ``obox.history.E203`` — ``fields``/``exclude``/``mask`` reference an unknown
  attribute.
* ``obox.history.W201`` — tracked model without ``VersionedMixin`` (lost-update
  risk when two writers edit one row).
* ``obox.history.W202`` — ``HISTORY_FAILURE_MODE='open'`` downgrades history
  failures to log entries.
"""

from __future__ import annotations

import sqlalchemy as sa

from obox_builder.apps import apps
from obox_builder.conf import settings
from obox_builder.contrib.history.tracker import declared_models
from obox_builder.core.checks import register
from obox_builder.core.checks.messages import CheckMessage, Error, Warning
from obox_builder.db.mixins import VersionedMixin
from obox_builder.db.router import router

TAG = "history"


@register(tags=(TAG,))
def check_history_models() -> list[CheckMessage]:
    messages: list[CheckMessage] = []
    for model, options in declared_models():
        label = getattr(model, "_model_label", "") or model.__module__
        name = f"{label}.{model.__name__}"
        mapper = sa.inspect(model)
        if len(mapper.primary_key) != 1:
            messages.append(
                Error(
                    f"Row history for {name} requires a single-column primary key.",
                    id="obox.history.E201",
                )
            )
        if not issubclass(model, VersionedMixin):
            messages.append(
                Warning(
                    f"{name}: tracked without VersionedMixin; concurrent writers "
                    "can silently overwrite each other (lost update).",
                    id="obox.history.W201",
                )
            )
        attrs = set(mapper.attrs.keys())
        declared = sorted(set(options.fields or ()) | set(options.exclude) | set(options.mask))
        for field in declared:
            if field not in attrs:
                messages.append(
                    Error(
                        f"{name}: __history__ references unknown field {field!r}.",
                        id="obox.history.E203",
                    )
                )
        for relation in options.relations:
            rel = mapper.relationships.get(relation)
            if rel is None or rel.secondary is None:
                messages.append(
                    Error(
                        f"{name}: __history__ relation {relation!r} must be a "
                        "many-to-many relationship.",
                        id="obox.history.E202",
                    )
                )
    if settings.HISTORY_FAILURE_MODE == "open":
        messages.append(
            Warning(
                "HISTORY_FAILURE_MODE is 'open': row history write failures are "
                "logged and swallowed instead of failing the transaction.",
                id="obox.history.W202",
            )
        )
    return messages


@register(tags=("config",))
def check_history_database() -> list[CheckMessage]:
    if not (
        apps.models_ready
        and settings.HISTORY_ENABLED
        and apps.is_installed("obox_builder.contrib.history")
    ):
        return []

    from obox_builder.contrib.history.models import RowChange

    history_database = router.db_for_write(RowChange)
    messages: list[CheckMessage] = []
    for model, _options in declared_models():
        model_database = router.db_for_write(model)
        if model_database != history_database:
            messages.append(
                Error(
                    f"History tracking for {model._model_label}.{model.__name__} and "
                    f"RowChange must use the same database; routes currently select "
                    f"{model_database!r} and {history_database!r}.",
                    hint="Route tracked models and RowChange to the same alias so history "
                    "is written with the model change.",
                    id="obox.E006",
                )
            )
    if settings.HISTORY_FAILURE_MODE == "open" and history_database != "default":
        messages.append(
            Error(
                "HISTORY_FAILURE_MODE='open' requires RowChange to use the default database.",
                hint="Route history to default or use HISTORY_FAILURE_MODE='closed'.",
                id="obox.E006",
            )
        )
    return messages
