from __future__ import annotations

import httpx
from obox_builder.db.session import session_scope

from tests.admin.conftest import csrf_for, login_session, make_user


async def test_token_admin_hides_secret_and_revokes(client: httpx.AsyncClient) -> None:
    from obox_builder.contrib.auth.tokens import create_token, verify_token

    owner_id = await make_user("token-admin", is_superuser=True)
    async with session_scope() as db:
        raw, token = await create_token(db, owner_id, "Machine", ["shop.product:read"])
        pk = token.id
        digest = token.token_hash
    await login_session(client, "token-admin")
    csrf = await csrf_for("token-admin")
    url = "/admin/auth/api_token"
    listing = await client.get(url)
    assert listing.status_code == 200
    assert "Machine" in listing.text
    detail = await client.get(f"{url}/{pk}")
    assert detail.status_code == 200
    assert raw not in detail.text
    assert digest not in detail.text
    assert (await client.get(f"{url}/add")).status_code == 403
    response = await client.post(
        f"{url}/actions",
        data={"csrf_token": csrf, "action": "revoke_tokens", "_selected_action": str(pk)},
    )
    assert response.status_code == 303, response.text
    async with session_scope() as db:
        assert await verify_token(db, raw) is None
