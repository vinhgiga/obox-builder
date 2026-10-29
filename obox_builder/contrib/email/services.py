"""Public helpers for sending email through the configured backend."""

from __future__ import annotations

from collections.abc import Sequence
from importlib import import_module
from typing import Any

from obox_builder.conf import settings
from obox_builder.contrib.email.backends import EmailBackend, EmailMessage
from obox_builder.exceptions import ImproperlyConfigured


def get_email_backend() -> EmailBackend:
    module_path, separator, name = settings.EMAIL_BACKEND.rpartition(".")
    if not separator:
        raise ImproperlyConfigured("EMAIL_BACKEND must be a dotted path.")
    try:
        backend_class: Any = getattr(import_module(module_path), name)
        backend = backend_class()
    except (ImportError, AttributeError, TypeError) as exc:
        raise ImproperlyConfigured(
            f"Cannot load email backend {settings.EMAIL_BACKEND!r}: {exc}"
        ) from exc
    if not callable(getattr(backend, "send_messages", None)):
        raise ImproperlyConfigured(
            f"Email backend {settings.EMAIL_BACKEND!r} must define send_messages()."
        )
    return backend


async def send_mail(
    subject: str,
    body: str,
    recipients: str | Sequence[str],
    *,
    from_email: str | None = None,
) -> int:
    """Send one plain-text message and return the number successfully delivered."""
    to = (recipients,) if isinstance(recipients, str) else tuple(recipients)
    if not to or any(not address.strip() for address in to):
        raise ValueError("Email recipients must contain at least one non-blank address.")
    message = EmailMessage(
        subject=subject,
        body=body,
        from_email=from_email or settings.DEFAULT_FROM_EMAIL,
        recipients=to,
    )
    return await get_email_backend().send_messages([message])
