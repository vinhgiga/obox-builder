from __future__ import annotations

import httpx
import pytest

from tests.admin.conftest import login_session, make_user

pytestmark = pytest.mark.usefixtures("admin_env")


async def test_anonymous_gets_redirect_to_login(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/", headers={"accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")


async def test_dashboard_renders_stats(client: httpx.AsyncClient) -> None:
    await make_user("alice")
    await login_session(client, "alice")
    response = await client.get("/admin/", headers={"accept": "text/html"})
    assert response.status_code == 200
    assert "Dashboard" in response.text
    assert "Welcome back, alice." in response.text
    assert "Users" in response.text


async def test_navigation_hides_unreachable_sections(client: httpx.AsyncClient) -> None:
    await make_user("alice")
    await login_session(client, "alice")
    response = await client.get("/admin/", headers={"accept": "text/html"})
    assert response.status_code == 200
    assert 'href="/admin/users"' not in response.text
    assert 'href="/admin/roles"' not in response.text
    assert "Change your password" in response.text
