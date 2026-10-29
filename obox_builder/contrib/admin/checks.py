"""System checks for ModelAdmin declarations (loaded by AdminConfig.ready)."""

from __future__ import annotations

from obox_builder.apps import apps
from obox_builder.conf import settings
from obox_builder.contrib.admin.site import site
from obox_builder.core.checks.messages import CheckMessage, Error, Warning
from obox_builder.core.checks.registry import register
from obox_builder.exceptions import ImproperlyConfigured

ADMIN = "admin"
COMMENTS_APP = "obox_builder.contrib.comments"


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
    if apps.apps_ready and apps.is_installed(COMMENTS_APP):
        from obox_builder.contrib.comments.options import parse_comments

        registered = {admin.model for admin in site.get_registry().values()}
        for model in apps.get_models():
            raw = getattr(model, "__comments__", None)
            if raw is None or raw is False:
                continue
            try:
                options = parse_comments(raw)
            except ImproperlyConfigured:
                continue
            if not options.enabled or model in registered:
                continue
            messages.append(
                Warning(
                    f"{model._model_label}.{model.__name__} declares __comments__ but is "
                    "not registered in the admin site; comments have no UI.",
                    id="obox.comments.W301",
                )
            )
    return messages


@register(tags=(ADMIN,))
def check_admin_api() -> list[CheckMessage]:
    from obox_builder.contrib.auth.csrf import _origin_tuple

    messages: list[CheckMessage] = []
    if any(
        _origin_tuple(origin, require_bare_origin=True) is None
        for origin in settings.ADMIN_API_TRUSTED_ORIGINS
    ):
        messages.append(
            Error(
                "ADMIN_API_TRUSTED_ORIGINS must contain explicit HTTP(S) origins.",
                id="obox.admin.E501",
            )
        )
    if (
        not settings.DEBUG
        and not settings.SESSION_COOKIE_SECURE
        and any(admin.api_enabled for admin in site.get_registry().values())
    ):
        messages.append(
            Warning(
                "The admin API requires secure session cookies in production.", id="obox.admin.W502"
            )
        )
    return messages
