"""System checks for ``Model.__comments__`` declarations.

* ``obox.comments.E301`` — commentable model has a composite primary key
  (comments address rows by one stringified key).
* ``obox.comments.E302`` — the ``__comments__`` mapping is invalid.
* ``obox.comments.E303`` — comments are installed without the auth app.
"""

from __future__ import annotations

import sqlalchemy as sa

from obox_builder.apps import apps
from obox_builder.contrib.comments.options import parse_comments
from obox_builder.core.checks import register
from obox_builder.core.checks.messages import CheckMessage, Error
from obox_builder.core.exceptions import ImproperlyConfigured

TAG = "comments"
AUTH_APP = "obox_builder.contrib.auth"


@register(tags=(TAG,))
def check_comments_models() -> list[CheckMessage]:
    messages: list[CheckMessage] = []
    if not apps.apps_ready or not apps.is_installed("obox_builder.contrib.comments"):
        return messages
    if not apps.is_installed(AUTH_APP):
        messages.append(
            Error(
                "Row comments require obox_builder.contrib.auth in INSTALLED_APPS.",
                id="obox.comments.E303",
            )
        )
        return messages
    for model in apps.get_models():
        raw = getattr(model, "__comments__", None)
        if raw is None or raw is False:
            continue
        name = f"{getattr(model, '_model_label', '')}.{model.__name__}"
        try:
            options = parse_comments(raw)
        except ImproperlyConfigured as exc:
            messages.append(Error(f"{name}: {exc}", id="obox.comments.E302"))
            continue
        if not options.enabled:
            continue
        if len(sa.inspect(model).primary_key) != 1:
            messages.append(
                Error(
                    f"Row comments for {name} require a single-column primary key.",
                    id="obox.comments.E301",
                )
            )
    return messages


__all__ = ["check_comments_models"]
