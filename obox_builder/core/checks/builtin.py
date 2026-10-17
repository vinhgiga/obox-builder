from __future__ import annotations

from obox_builder.conf import settings
from obox_builder.core.checks.messages import CheckMessage, Error, Info, Warning
from obox_builder.core.checks.registry import register
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.utils import import_string

SECURITY = "security"
CONFIG = "config"
INSECURE_SECRET_MARKERS = ("django-insecure", "changeme", "insecure", "dev-")
MINIMUM_SECRET_KEY_LENGTH = 32


@register(tags=(SECURITY,))
def check_secret_key() -> list[CheckMessage]:
    value = settings.SECRET_KEY.get_secret_value()
    if len(value) < MINIMUM_SECRET_KEY_LENGTH:
        return [
            Error(
                f"SECRET_KEY is too short ({len(value)} characters).",
                hint=f"Use at least {MINIMUM_SECRET_KEY_LENGTH} characters from a secure "
                "random source. The secret key is used for signing and CSRF protection.",
                id="obox.E001",
            )
        ]
    return []


@register(tags=(SECURITY,), deploy=True)
def check_secret_key_deploy() -> list[CheckMessage]:
    value = settings.SECRET_KEY.get_secret_value().lower()
    if any(marker in value for marker in INSECURE_SECRET_MARKERS):
        return [
            Error(
                "SECRET_KEY looks like a development or placeholder value.",
                hint="Generate a production secret key and provide it through the "
                "OBOX_SECRET_KEY environment variable.",
                id="obox.E002",
            )
        ]
    return []


@register(tags=(SECURITY,), deploy=True)
def check_debug_deploy() -> list[CheckMessage]:
    if settings.DEBUG:
        return [
            Error(
                "DEBUG is enabled.",
                hint="Set OBOX_DEBUG=false in production.",
                id="obox.E003",
            )
        ]
    return []


@register(tags=(CONFIG,))
def check_installed_apps() -> list[CheckMessage]:
    if not settings.INSTALLED_APPS:
        return [
            Warning(
                "INSTALLED_APPS is empty.",
                hint="Install at least 'obox_builder.contrib.admin' to get an admin interface.",
                id="obox.W001",
            )
        ]
    return []


@register(tags=(CONFIG,))
def check_template_dirs() -> list[CheckMessage]:
    missing = [str(path) for path in settings.TEMPLATE_DIRS if not path.is_dir()]
    if missing:
        return [
            Warning(
                f"TEMPLATE_DIRS contains directories that do not exist: {', '.join(missing)}.",
                hint="Fix the TEMPLATE_DIRS setting or create the directories.",
                id="obox.W002",
            )
        ]
    return []


@register(tags=(SECURITY,), deploy=True)
def check_session_cookie_secure() -> list[CheckMessage]:
    if not settings.SESSION_COOKIE_SECURE:
        return [
            Warning(
                "SESSION_COOKIE_SECURE is disabled.",
                hint="Set OBOX_SESSION_COOKIE_SECURE=true when serving over HTTPS.",
                id="obox.W003",
            )
        ]
    return []


@register(tags=(CONFIG,))
def check_middleware() -> list[CheckMessage]:
    messages: list[CheckMessage] = []
    for entry in settings.MIDDLEWARE:
        try:
            import_string(entry)
        except ImproperlyConfigured as exc:
            messages.append(
                Error(f"Middleware entry {entry!r} could not be imported: {exc}", id="obox.E004")
            )
    return messages


@register(tags=(CONFIG,))
def check_root_urlconf() -> list[CheckMessage]:
    if not settings.ROOT_URLCONF:
        return [
            Info(
                "ROOT_URLCONF is not set; no URL routes will be registered.",
                hint="Set ROOT_URLCONF to a module defining `routers`.",
                id="obox.I001",
            )
        ]
    return []
