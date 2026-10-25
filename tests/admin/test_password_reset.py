from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
from obox_builder.conf import settings
from obox_builder.contrib.auth.services import create_user
from obox_builder.db.session import session_scope

from tests.support import email_backend


async def test_reset_request_response_does_not_reveal_account_existence(
    client: httpx.AsyncClient,
) -> None:
    config = settings.load()
    settings.configure(
        config.model_copy(
            update={
                "EMAIL_BACKEND": "tests.support.email_backend.MemoryBackend",
                "PASSWORD_RESET_COOLDOWN": 0,
            }
        )
    )
    email_backend.MESSAGES.clear()
    async with session_scope() as db:
        await create_user(db, "alice", "s3cure-password", email="alice@example.org")

    existing = await client.post("/admin/password_reset", data={"email": "alice@example.org"})
    assert existing.status_code == 200
    assert "If an active account matches that email" in existing.text
    assert len(email_backend.MESSAGES) == 1
    token = parse_qs(urlsplit(email_backend.MESSAGES[0].body.splitlines()[2]).query)["token"][0]

    missing = await client.post("/admin/password_reset", data={"email": "missing@example.org"})
    assert missing.status_code == existing.status_code
    assert missing.text == existing.text
    assert len(email_backend.MESSAGES) == 1

    page = await client.get("/admin/password_reset/confirm", params={"token": token})
    assert page.status_code == 200
    assert "Choose a new password" in page.text
