"""Deployment checks for independent REST APIs."""

from obox_builder.conf import settings
from obox_builder.contrib.auth.csrf import _origin_tuple
from obox_builder.core.checks.messages import CheckMessage, Error, Warning
from obox_builder.core.checks.registry import register


@register(tags=("api",))
def check_api() -> list[CheckMessage]:
    messages: list[CheckMessage] = []
    if any(
        _origin_tuple(origin, require_bare_origin=True) is None
        for origin in settings.API_TRUSTED_ORIGINS
    ):
        messages.append(
            Error("API_TRUSTED_ORIGINS must contain explicit HTTP(S) origins.", id="obox.api.E001")
        )
    if not settings.DEBUG and not settings.SESSION_COOKIE_SECURE:
        messages.append(
            Warning("The API requires secure session cookies in production.", id="obox.api.W002")
        )
    return messages
