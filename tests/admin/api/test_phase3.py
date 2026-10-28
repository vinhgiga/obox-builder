from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.conf import settings
from obox_builder.contrib.admin import action
from obox_builder.core.utils.time import utcnow
from obox_builder.db.session import session_scope
from tests.admin.api.conftest import sign_in, site_for
from tests.admin.api.test_records import BASE, ITEMS, create_item
from tests.admin.conftest import grant_permissions


async def mint(
    api: httpx.AsyncClient, headers: dict[str, str], scopes: list[str], **extra: Any
) -> dict[str, Any]:
    response = await api.post(
        f"{BASE}/tokens", headers=headers, json={"name": "Integration", "scopes": scopes, **extra}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_pat_scopes_audit_and_revocation(api: httpx.AsyncClient) -> None:
    from obox_builder.contrib.auth.models import ApiToken
    from obox_builder.contrib.history.models import RowChange

    headers = await sign_in(api)
    token = await mint(api, headers, ["warehouse.item:read", "warehouse.item:write"])
    bearer = {"Authorization": f"Bearer {token['token']}"}
    created = await create_item(api, bearer)
    assert created.json()["meta"]["can_change"] is True
    assert created.json()["meta"]["can_delete"] is False
    assert (await api.get(f"{BASE}/models/shop.product/records", headers=bearer)).status_code == 404
    assert (await api.get(ITEMS, headers=bearer)).status_code == 200
    me = await api.get(f"{BASE}/auth/me", headers=bearer)
    assert me.status_code == 200
    assert me.json()["csrf_token"] == ""
    assert me.json()["scopes"] == token["scopes"]
    for method, path in (
        ("GET", "/tokens"),
        ("POST", "/tokens"),
        ("POST", "/auth/password"),
        ("POST", "/auth/logout"),
    ):
        assert (
            await api.request(method, f"{BASE}{path}", headers=bearer, json={})
        ).status_code == 403
    listing = await api.get(f"{BASE}/tokens")
    assert "token_hash" not in listing.text
    assert token["token"] not in listing.text
    async with session_scope() as db:
        row = await db.get(ApiToken, int(token["id"]))
        assert row is not None
        assert row.token_hash != token["token"]
        assert row.last_used_at is not None
        changes = (
            await db.scalars(sa.select(RowChange).where(RowChange.model_label == "warehouse.item"))
        ).all()
        assert changes[0].actor_label == f"api-user via {token['token_prefix']}"
        assert changes[0].source == "api"
    assert (await api.delete(f"{BASE}/tokens/{token['id']}", headers=headers)).status_code == 204
    denied = await api.get(ITEMS, headers=bearer)
    assert denied.status_code == 401
    assert denied.headers["www-authenticate"].startswith("Bearer")


@pytest.mark.parametrize(
    "scopes,expected",
    [
        (["warehouse.item:read"], 403),
        (["warehouse.item:read", "warehouse.item:write"], 201),
        (["warehouse.*:*"], 201),
        (["*:read"], 403),
    ],
)
async def test_scope_matrix(api: httpx.AsyncClient, scopes: list[str], expected: int) -> None:
    headers = await sign_in(api)
    token = await mint(api, headers, scopes)
    response = await api.post(
        ITEMS,
        headers={"Authorization": f"Bearer {token['token']}"},
        json={"fields": {"name": "Scoped", "sku": "scope"}},
    )
    assert response.status_code == expected, response.text


async def test_pat_cannot_exceed_user_permissions_or_use_cookie_fallback(
    api: httpx.AsyncClient,
) -> None:
    headers = await sign_in(api, superuser=False)
    await grant_permissions("api-user", "warehouse.item.read")
    token = await mint(api, headers, ["warehouse.item:*"])
    bearer = {"Authorization": f"Bearer {token['token']}"}
    assert (await api.get(ITEMS, headers=bearer)).status_code == 200
    assert (
        await api.post(ITEMS, headers=bearer, json={"fields": {"name": "No", "sku": "no"}})
    ).status_code == 403
    for authorization in ("Bearer invalid", "Basic invalid", "Bearer"):
        response = await api.get(ITEMS, headers={"Authorization": authorization})
        assert response.status_code == 401


async def test_pat_expiry_deactivation_owner_isolation_and_last_used(
    api: httpx.AsyncClient,
) -> None:
    from obox_builder.contrib.auth.models import ApiToken, User

    headers = await sign_in(api)
    token = await mint(api, headers, ["warehouse.item:read"])
    bearer = {"Authorization": f"Bearer {token['token']}"}
    assert (await api.get(ITEMS, headers=bearer)).status_code == 200
    async with session_scope() as db:
        row = await db.get(ApiToken, int(token["id"]))
        assert row is not None
        stamp = row.last_used_at
    assert (await api.get(ITEMS, headers=bearer)).status_code == 200
    async with session_scope() as db:
        row = await db.get(ApiToken, int(token["id"]))
        assert row is not None
        assert row.last_used_at == stamp
        owner_id = row.user_id
        row.expires_at = utcnow() - timedelta(seconds=1)
    assert (await api.get(ITEMS, headers=bearer)).status_code == 401
    async with session_scope() as db:
        row = await db.get(ApiToken, int(token["id"]))
        assert row is not None
        row.expires_at = utcnow() + timedelta(days=1)
        owner = await db.get(User, owner_id)
        assert owner is not None
        owner.is_active = False
    assert (await api.get(ITEMS, headers=bearer)).status_code == 401
    other = await sign_in(api, username="other-owner")
    assert (await api.get(f"{BASE}/tokens")).json()["tokens"] == []
    assert (await api.delete(f"{BASE}/tokens/{token['id']}", headers=other)).status_code == 404


@pytest.mark.parametrize(
    "scope",
    [
        "unknown.model:read",
        "warehouse.item:admin",
        "warehouse.item",
        "*",
        "warehouse.item:read:write",
    ],
)
async def test_invalid_token_scopes(api: httpx.AsyncClient, scope: str) -> None:
    headers = await sign_in(api)
    response = await api.post(
        f"{BASE}/tokens", headers=headers, json={"name": "Invalid", "scopes": [scope]}
    )
    assert response.status_code == 422


async def test_pat_relation_scope_and_idempotency_principals(api: httpx.AsyncClient) -> None:
    from tests.support.apps.warehouse.models import Shelf

    headers = await sign_in(api)
    async with session_scope() as db:
        shelf = Shelf(code="hidden")
        db.add(shelf)
        await db.flush()
        shelf_id = str(shelf.id)
    await create_item(api, headers, shelf_id=shelf_id)
    token = await mint(api, headers, ["warehouse.item:*"])
    bearer = {"Authorization": f"Bearer {token['token']}", "Idempotency-Key": "same-key"}
    listing = (await api.get(ITEMS, headers=bearer)).json()
    assert listing["records"][0]["fields"]["shelf_id"] is None
    assert (await api.get(f"{BASE}/models/warehouse.item/options/shelf_id", headers=bearer)).json()[
        "options"
    ] == []
    blocked = await api.post(
        ITEMS,
        headers=bearer,
        json={"fields": {"name": "Blocked", "sku": "blocked", "shelf_id": shelf_id}},
    )
    assert blocked.status_code == 422
    a = await api.post(
        ITEMS,
        headers={**headers, "Idempotency-Key": "same-key"},
        json={"fields": {"name": "Session", "sku": "session"}},
    )
    b = await api.post(ITEMS, headers=bearer, json={"fields": {"name": "Token", "sku": "token"}})
    assert a.status_code == b.status_code == 201
    assert a.json()["id"] != b.json()["id"]


async def test_upsert_creation_requires_etag_for_existing_and_is_atomic(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.support.apps.warehouse.models import Item

    headers = await sign_in(api)
    admin = site_for(api).get_model_admin_for(Item)
    monkeypatch.setattr(admin, "import_keys", ("sku",))
    url = f"{ITEMS}:upsert"
    payload: dict[str, Any] = {
        "merge_on": ["sku"],
        "records": [{"fields": {"name": "New", "sku": "sku"}}],
    }
    created = await api.patch(url, headers=headers, json=payload)
    assert created.status_code == 200, created.text
    record = created.json()["records"][0]
    assert record["created"] is True
    assert (await api.patch(url, headers=headers, json=payload)).status_code == 428
    payload["records"][0]["etag"] = record["meta"]["etag"]
    payload["records"][0]["fields"]["name"] = "Updated"
    updated = await api.patch(url, headers=headers, json=payload)
    assert updated.status_code == 200, updated.text
    assert updated.json()["records"][0]["created"] is False
    assert (await api.patch(url, headers=headers, json=payload)).status_code == 412
    atomic = {
        "merge_on": ["sku"],
        "records": [
            {"fields": {"name": "New second", "sku": "a-new"}},
            {"fields": {"sku": "sku", "name": "Old without etag"}},
        ],
    }
    assert (await api.patch(url, headers=headers, json=atomic)).status_code == 428
    assert len((await api.get(ITEMS)).json()["records"]) == 1
    for keys in (["name"], ["sku", "name"], [], ["sku", "sku"]):
        assert (
            await api.patch(url, headers=headers, json={**payload, "merge_on": keys})
        ).status_code == 422


async def test_upsert_requires_unique_keys_and_respects_scope(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.support.apps.warehouse.models import Item

    headers = await sign_in(api)
    admin = site_for(api).get_model_admin_for(Item)
    monkeypatch.setattr(admin, "import_keys", ("name",))
    response = await api.patch(
        f"{ITEMS}:upsert",
        headers=headers,
        json={"merge_on": ["name"], "records": [{"fields": {"name": "Nonunique", "sku": "sku"}}]},
    )
    assert response.status_code == 422
    token = await mint(api, headers, ["warehouse.item:read"])
    monkeypatch.setattr(admin, "import_keys", ("sku",))
    denied = await api.patch(
        f"{ITEMS}:upsert",
        headers={"Authorization": f"Bearer {token['token']}"},
        json={"merge_on": ["sku"], "records": [{"fields": {"name": "Denied", "sku": "sku"}}]},
    )
    assert denied.status_code == 403


async def test_actions_opt_in_scope_idempotency_and_rollback(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.responses import HTMLResponse
    from tests.support.apps.warehouse.models import Item

    headers = await sign_in(api)
    record = (await create_item(api, headers)).json()
    admin = site_for(api).get_model_admin_for(Item)

    @action(api=True, permissions=("change",))
    async def publish(self: Any, ctx: Any, queryset: Any) -> Any:
        for obj in await ctx.db.scalars(queryset):
            obj.quantity += 1
        return {"published": True}

    @action(api=True, permissions=("change",))
    async def invalid(self: Any, ctx: Any, queryset: Any) -> Any:
        for obj in await ctx.db.scalars(queryset):
            obj.name = "Must roll back"
        return HTMLResponse("HTML is not an API result")

    monkeypatch.setattr(type(admin), "publish", publish, raising=False)
    monkeypatch.setattr(type(admin), "invalid", invalid, raising=False)
    monkeypatch.setattr(admin, "actions", ("publish", "invalid"))
    url = f"{BASE}/models/warehouse.item/actions"
    assert (
        await api.post(f"{url}/delete_selected", headers=headers, json={"ids": [record["id"]]})
    ).status_code == 404
    for _ in range(2):
        response = await api.post(
            f"{url}/publish",
            headers={**headers, "Idempotency-Key": "action"},
            json={"ids": [record["id"]]},
        )
        assert response.status_code == 200, response.text
    token = await mint(api, headers, ["warehouse.item:read"])
    assert (
        await api.post(
            f"{url}/publish",
            headers={"Authorization": f"Bearer {token['token']}"},
            json={"select_across": True},
        )
    ).status_code == 404
    assert (
        await api.post(f"{url}/invalid", headers=headers, json={"ids": [record["id"]]})
    ).status_code == 422
    current = (await api.get(f"{ITEMS}/{record['id']}")).json()
    assert current["fields"]["quantity"] == 1
    assert current["fields"]["name"] == "Original"


async def test_saved_views_are_private_and_reusable(api: httpx.AsyncClient) -> None:
    headers = await sign_in(api)
    await create_item(api, headers)
    url = f"{BASE}/models/warehouse.item/views"
    saved = await api.post(
        url, headers=headers, json={"name": "Mine", "config": {"q": "Original", "sort": "sku"}}
    )
    assert saved.status_code == 201, saved.text
    pk = saved.json()["id"]
    renamed = await api.post(
        url, headers=headers, json={"id": pk, "name": "Renamed", "config": {"q": "Original"}}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["id"] == pk
    assert len((await api.get(url)).json()["views"]) == 1
    assert (await api.get(ITEMS, params={"view": pk})).json()["records"][0]["fields"][
        "name"
    ] == "Original"
    assert (
        await api.post(
            url, headers=headers, json={"name": "Bad", "config": {"sort": "secret_token"}}
        )
    ).status_code == 400
    other = await sign_in(api, username="other")
    assert (await api.get(url)).json()["views"] == []
    assert (await api.delete(f"{url}/{pk}", headers=other)).status_code == 404
    assert (
        await api.post(url, headers=other, json={"id": pk, "name": "Stolen", "config": {}})
    ).status_code == 404
    assert (await api.get(ITEMS, params={"view": pk})).status_code == 404


async def test_form_state_rules_and_no_persistence(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from tests.support.apps.warehouse.models import Item

    headers = await sign_in(api)
    admin = site_for(api).get_model_admin_for(Item)
    original = admin.get_form_fields

    def fields(ctx: Any) -> Any:
        return [
            replace(
                spec,
                show_if=lambda ctx: ctx.values.get("quantity", 0) > 0,
                required_if=lambda ctx: ctx.values.get("quantity", 0) > 0,
                initial=lambda ctx: "Initial note",
            )
            if spec.name == "notes"
            else spec
            for spec in original(ctx)
        ]

    monkeypatch.setattr(admin, "get_form_fields", fields)
    url = f"{ITEMS}/_form-state"
    state = await api.post(
        url, headers=headers, json={"fields": {"name": "Draft", "sku": "draft", "quantity": 1}}
    )
    assert state.status_code == 200, state.text
    notes = state.json()["fields"]["notes"]
    assert notes["visible"] is True
    assert notes["required"] is True
    assert notes["initial"] == "Initial note"
    assert "notes" in state.json()["errors"]
    assert "secret_token" not in state.json()["fields"]
    assert (await api.get(ITEMS)).json()["records"] == []


async def test_concurrent_upsert_creates_one_row(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.support.apps.warehouse.models import Item

    if settings.DATABASE_URL.startswith("sqlite"):
        pytest.skip("Concurrent requests use PostgreSQL connections.")
    headers = await sign_in(api)
    monkeypatch.setattr(site_for(api).get_model_admin_for(Item), "import_keys", ("sku",))
    payload = {"merge_on": ["sku"], "records": [{"fields": {"name": "Once", "sku": "once"}}]}
    responses = await asyncio.gather(
        *(api.patch(f"{ITEMS}:upsert", headers=headers, json=payload) for _ in range(2))
    )
    assert sorted(response.status_code for response in responses) == [200, 428]
    assert len((await api.get(ITEMS)).json()["records"]) == 1


async def test_action_permission_alternatives_intersect_token_scope(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.support.apps.warehouse.models import Item

    headers = await sign_in(api, superuser=False)
    await grant_permissions("api-user", "warehouse.item.read", "warehouse.item.delete")
    admin = site_for(api).get_model_admin_for(Item)

    @action(api=True, permissions=("change", "delete"))
    async def mixed(self: Any, ctx: Any, queryset: Any) -> None:
        raise AssertionError("An unauthorized action must never run.")

    monkeypatch.setattr(type(admin), "mixed", mixed, raising=False)
    monkeypatch.setattr(admin, "actions", ("mixed",))
    token = await mint(api, headers, ["warehouse.item:read", "warehouse.item:write"])
    response = await api.post(
        f"{BASE}/models/warehouse.item/actions/mixed",
        headers={"Authorization": f"Bearer {token['token']}"},
        json={"select_across": True},
    )
    assert response.status_code == 404


async def test_concurrent_pat_usage_updates_timestamp_once(api: httpx.AsyncClient) -> None:
    from obox_builder.contrib.auth.models import ApiToken

    if settings.DATABASE_URL.startswith("sqlite"):
        pytest.skip("Concurrent token usage uses PostgreSQL connections.")
    headers = await sign_in(api)
    token = await mint(api, headers, ["warehouse.item:read"])
    bearer = {"Authorization": f"Bearer {token['token']}"}
    for expected_version in (2, 4):
        responses = await asyncio.gather(*(api.get(ITEMS, headers=bearer) for _ in range(2)))
        assert [response.status_code for response in responses] == [200, 200]
        async with session_scope() as db:
            row = await db.get(ApiToken, int(token["id"]))
            assert row is not None
            assert row.version == expected_version
            row.last_used_at = utcnow() - timedelta(minutes=2)
