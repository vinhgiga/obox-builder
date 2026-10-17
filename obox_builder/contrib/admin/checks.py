"""System checks for ModelAdmin declarations (loaded by AdminConfig.ready)."""

from __future__ import annotations

from obox_builder.contrib.admin.site import site
from obox_builder.core.checks.messages import CheckMessage, Error
from obox_builder.core.checks.registry import register

ADMIN = "admin"


@register(tags=(ADMIN,))
def check_model_admins() -> list[CheckMessage]:
    """Validate every registered ModelAdmin (fields, search, filters, ordering)."""
    site.autodiscover()
    messages: list[CheckMessage] = []
    paths: dict[str, str] = {}
    for admin in site.get_registry().values():
        messages.extend(admin.validate())
        path = admin.list_path()
        previous = paths.get(path)
        if previous is not None:
            messages.append(
                Error(
                    f"Models {previous!r} and {admin.model_label!r} are both registered "
                    f"at {path!r}; URLs would collide.",
                    id="obox.admin.E101",
                )
            )
        paths[path] = admin.model_label
    return messages
