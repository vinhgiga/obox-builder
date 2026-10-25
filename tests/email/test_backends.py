from __future__ import annotations

from typing import Any

import pytest
from obox_builder.conf import BaseSettings, settings
from obox_builder.contrib.email import send_mail
from pydantic import SecretStr


def configure_email(**overrides: object) -> BaseSettings:
    config = BaseSettings(
        SECRET_KEY=SecretStr("email-tests-secret-key-0123456789abcdef"),
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
    ).model_copy(update=overrides)
    settings.configure(config)
    return config


async def test_configured_backend_receives_email(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.support import email_backend

    email_backend.MESSAGES.clear()
    config = configure_email(EMAIL_BACKEND="tests.support.email_backend.MemoryBackend")

    assert await send_mail("Hello", "Body", "person@example.com") == 1
    assert email_backend.MESSAGES[0].subject == "Hello"
    assert email_backend.MESSAGES[0].body == "Body"
    assert email_backend.MESSAGES[0].recipients == ("person@example.com",)
    assert email_backend.MESSAGES[0].from_email == config.DEFAULT_FROM_EMAIL


async def test_smtp_backend_uses_starttls_before_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import smtplib

    calls: list[Any] = []

    class SMTPStub:
        def __init__(self, host: str, port: int, *, timeout: float) -> None:
            calls.append(("connect", host, port, timeout))

        def __enter__(self) -> SMTPStub:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def ehlo(self) -> None:
            calls.append(("ehlo",))

        def starttls(self, *, context: object) -> None:
            calls.append(("starttls", context))

        def login(self, username: str, password: str) -> None:
            calls.append(("login", username, password))

        def send_message(self, message: object) -> dict[str, str]:
            calls.append(("send", message))
            return {}

    monkeypatch.setattr(smtplib, "SMTP", SMTPStub)
    config = configure_email(
        EMAIL_BACKEND="obox_builder.contrib.email.backends.SMTPBackend",
        EMAIL_HOST="mail.example.com",
        EMAIL_PORT=587,
        EMAIL_HOST_USER="mailer",
        EMAIL_HOST_PASSWORD=SecretStr("smtp-password"),
        EMAIL_USE_TLS=True,
    )

    assert await send_mail("Hello", "Body", "person@example.com") == 1
    assert calls[0] == ("connect", "mail.example.com", 587, config.EMAIL_TIMEOUT)
    names = [call[0] for call in calls]
    assert names.index("starttls") < names.index("login") < names.index("send")


async def test_send_mail_rejects_empty_recipients() -> None:
    with pytest.raises(ValueError, match="at least one"):
        await send_mail("Hello", "Body", [])
