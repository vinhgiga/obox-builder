"""Console and SMTP delivery backends."""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from collections.abc import Sequence
from dataclasses import dataclass
from email.message import EmailMessage as MIMEEmailMessage
from typing import Protocol

from obox_builder.conf import settings
from obox_builder.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailMessage:
    subject: str
    body: str
    from_email: str
    recipients: tuple[str, ...]


class EmailBackend(Protocol):
    async def send_messages(self, messages: Sequence[EmailMessage]) -> int: ...


class ConsoleBackend:
    """Log outgoing messages for local development; configure SMTP in production."""

    async def send_messages(self, messages: Sequence[EmailMessage]) -> int:
        for message in messages:
            logger.info(
                "Email to %s\nFrom: %s\nSubject: %s\n\n%s",
                ", ".join(message.recipients),
                message.from_email,
                message.subject,
                message.body,
            )
        return len(messages)


class SMTPBackend:
    """Send plain-text email through SMTP without blocking the event loop."""

    async def send_messages(self, messages: Sequence[EmailMessage]) -> int:
        if not messages:
            return 0
        return await asyncio.to_thread(self._send_messages, tuple(messages))

    def _send_messages(self, messages: Sequence[EmailMessage]) -> int:
        if not settings.EMAIL_HOST.strip():
            raise ImproperlyConfigured("EMAIL_HOST must be configured for SMTP delivery.")
        context = ssl.create_default_context()
        if settings.EMAIL_USE_SSL:
            connection: smtplib.SMTP = smtplib.SMTP_SSL(
                settings.EMAIL_HOST,
                settings.EMAIL_PORT,
                timeout=settings.EMAIL_TIMEOUT,
                context=context,
            )
        else:
            connection = smtplib.SMTP(
                settings.EMAIL_HOST, settings.EMAIL_PORT, timeout=settings.EMAIL_TIMEOUT
            )
        sent = 0
        with connection:
            connection.ehlo()
            if settings.EMAIL_USE_TLS:
                connection.starttls(context=context)
                connection.ehlo()
            password = settings.EMAIL_HOST_PASSWORD.get_secret_value()
            if settings.EMAIL_HOST_USER and password:
                connection.login(settings.EMAIL_HOST_USER, password)
            for message in messages:
                mime_message = MIMEEmailMessage()
                mime_message["Subject"] = message.subject
                mime_message["From"] = message.from_email
                mime_message["To"] = ", ".join(message.recipients)
                mime_message.set_content(message.body)
                refused = connection.send_message(mime_message)
                if not refused:
                    sent += 1
        return sent
