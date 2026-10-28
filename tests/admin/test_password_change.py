from __future__ import annotations

import httpx
import pytest
import sqlalchemy as sa

from tests.admin.conftest import csrf_for, login_session, make_user

pytestmark = pytest.mark.usefixtures("admin_env")


async def test_password_change_page_requires_login(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/admin/password_change", headers={"accept": "text/html"}, follow_redirects=False
    )
    assert response.status_code == 303


async def test_password_change_keeps_current_session_and_revokes_others(
    client: httpx.AsyncClient,
) -> None:
    from obox_builder.conf import settings
    from obox_builder.contrib.auth import services, sessions
    from obox_builder.contrib.auth.models import Session as AuthSession
    from obox_builder.contrib.auth.models import User
    from obox_builder.db.session import session_scope

    await make_user("alice")
    old_token = await login_session(client, "alice")
    async with session_scope() as db:
        alice = await db.scalar(sa.select(User).where(User.username == "alice"))
        assert alice is not None
        await sessions.create_session(db, alice.id)

    token = await csrf_for("alice", old_token)
    response = await client.post(
        "/admin/password_change",
        data={
            "csrf_token": token,
            "current_password": "s3cure-password",
            "new_password": "brand-new-secret",
            "confirm_password": "brand-new-secret",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    set_cookie = response.headers["set-cookie"]
    new_token = set_cookie.split("obox_sid=", 1)[1].split(";", 1)[0]
    assert new_token != old_token
    assert settings.SESSION_COOKIE_NAME == "obox_sid"

    async with session_scope() as db:
        rows = list(await db.scalars(sa.select(AuthSession)))
        assert len(rows) == 1
        assert rows[0].token_hash == sessions.hash_token(new_token)
        assert await services.authenticate(db, "alice", "brand-new-secret") is not None

    dashboard = await client.get("/admin/", headers={"accept": "text/html"})
    assert dashboard.status_code == 200
    assert "Your password has been changed." in dashboard.text


async def test_password_change_rejects_wrong_current_password(
    client: httpx.AsyncClient,
) -> None:
    await make_user("alice")
    current = await login_session(client, "alice")
    token = await csrf_for("alice", current)
    response = await client.post(
        "/admin/password_change",
        data={
            "csrf_token": token,
            "current_password": "wrong-password",
            "new_password": "brand-new-secret",
            "confirm_password": "brand-new-secret",
        },
    )
    assert response.status_code == 200
    assert "Current password is incorrect" in response.text


async def test_password_change_rejects_weak_and_mismatched(
    client: httpx.AsyncClient,
) -> None:
    await make_user("alice")
    current = await login_session(client, "alice")
    token = await csrf_for("alice", current)
    mismatched = await client.post(
        "/admin/password_change",
        data={
            "csrf_token": token,
            "current_password": "s3cure-password",
            "new_password": "brand-new-secret",
            "confirm_password": "other-secret-1",
        },
    )
    assert mismatched.status_code == 200
    assert "do not match" in mismatched.text

    weak = await client.post(
        "/admin/password_change",
        data={
            "csrf_token": token,
            "current_password": "s3cure-password",
            "new_password": "short",
            "confirm_password": "short",
        },
    )
    assert weak.status_code == 200
    assert "at least" in weak.text


async def test_password_change_preserves_custom_backend(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obox_builder.conf import settings

    from tests.admin.conftest import form_login

    assert settings._wrapped is not None
    model = "obox_builder.contrib.auth.backends.ModelBackend"
    custom = "tests.support.auth_backend.PasswordBackend"
    monkeypatch.setattr(settings._wrapped, "AUTHENTICATION_BACKENDS", [model, custom])
    await make_user("alice")
    login = await form_login(client, "alice", "backend-secret")
    assert login.status_code == 303
    token = await csrf_for("alice", client.cookies.get(settings.SESSION_COOKIE_NAME))
    changed = await client.post(
        "/admin/password_change",
        data={
            "csrf_token": token,
            "current_password": "s3cure-password",
            "new_password": "brand-new-secret",
            "confirm_password": "brand-new-secret",
        },
    )
    assert changed.status_code == 303
    assert (await client.get("/admin/", headers={"accept": "text/html"})).status_code == 200
    monkeypatch.setattr(settings._wrapped, "AUTHENTICATION_BACKENDS", [model])
    assert (await client.get("/admin/", headers={"accept": "text/html"})).status_code == 303
