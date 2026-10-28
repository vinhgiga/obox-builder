from __future__ import annotations

from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.conf import SETTINGS_MODULE_ENV
from obox_builder.db.session import session_scope
from tests.admin.api.conftest import sign_in, site_for
from tests.admin.api.test_phase3 import mint
from tests.admin.api.test_records import BASE, ITEMS, create_item
from tests.admin.conftest import grant_permissions


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import obox_builder

    monkeypatch.setenv(SETTINGS_MODULE_ENV, "tests.support.api_settings")
    obox_builder.setup()


async def permission_id(name: str) -> str:
    from obox_builder.contrib.auth.models import Permission

    service, resource, action = name.split(".")
    async with session_scope() as db:
        value = await db.scalar(
            sa.select(Permission.id).where(
                Permission.service == service,
                Permission.resource == resource,
                Permission.action == action,
            )
        )
        assert value is not None
        return str(value)


async def test_accounts_switch_crud_etags_and_secret_redaction(api: httpx.AsyncClient) -> None:
    from obox_builder.contrib.auth.hashers import verify_password
    from obox_builder.contrib.auth.models import User

    headers = await sign_in(api)
    assert (await api.get(f"{BASE}/users")).status_code == 404
    site_for(api).api_auth_enabled = True
    permission = await permission_id("shop.product.read")
    role = await api.post(
        f"{BASE}/roles", headers=headers, json={"name": "Reader", "permission_ids": [permission]}
    )
    assert role.status_code == 201, role.text
    row = role.json()
    assert set(row) == {"id", "name", "permission_ids", "etag"}
    created = await api.post(
        f"{BASE}/users",
        headers=headers,
        json={
            "username": "created",
            "password": "long-and-secure-password",
            "role_ids": [row["id"]],
        },
    )
    assert created.status_code == 201, created.text
    target = created.json()
    assert target["role_ids"] == [row["id"]]
    assert "password" not in created.text
    assert "hash" not in created.text
    path = f"{BASE}/users/{target['id']}"
    assert (await api.patch(path, headers=headers, json={"email": "a@b.test"})).status_code == 428
    changed = await api.patch(
        path,
        headers={**headers, "If-Match": created.headers["etag"]},
        json={"email": "A@B.test", "password": "another-secure-password"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["email"] == "a@b.test"
    assert (
        await api.patch(
            path, headers={**headers, "If-Match": created.headers["etag"]}, json={"email": "old"}
        )
    ).status_code == 412
    assert (
        await api.patch(
            path,
            headers={**headers, "If-Match": changed.headers["etag"]},
            json={"password_hash": "unsafe"},
        )
    ).status_code == 422
    async with session_scope() as db:
        user = await db.get(User, int(target["id"]))
        assert user
        assert verify_password("another-secure-password", user.password_hash)
    updated = await api.patch(
        f"{BASE}/roles/{row['id']}",
        headers={**headers, "If-Match": row["etag"]},
        json={"permission_ids": []},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["etag"] != row["etag"]
    assert (
        await api.delete(
            f"{BASE}/roles/{row['id']}", headers={**headers, "If-Match": updated.headers["etag"]}
        )
    ).status_code == 204
    assert (await api.get(path)).json()["role_ids"] == []


@pytest.mark.parametrize("field,value", [("description", "Legacy"), ("is_system", True)])
async def test_role_api_rejects_removed_fields(
    api: httpx.AsyncClient, field: str, value: Any
) -> None:
    headers = await sign_in(api)
    site_for(api).api_auth_enabled = True
    rejected = await api.post(
        f"{BASE}/roles", headers=headers, json={"name": "Reader", field: value}
    )
    assert rejected.status_code == 422
    assert (await api.get(f"{BASE}/roles")).json()["roles"] == []
    created = await api.post(f"{BASE}/roles", headers=headers, json={"name": "Reader"})
    assert created.status_code == 201
    row = created.json()
    path = f"{BASE}/roles/{row['id']}"
    rejected = await api.patch(
        path, headers={**headers, "If-Match": row["etag"]}, json={field: value}
    )
    assert rejected.status_code == 422
    assert (await api.get(path)).json() == row


async def test_delegation_policy_applies_to_api_and_html(api: httpx.AsyncClient) -> None:
    headers = await sign_in(api)
    site_for(api).api_auth_enabled = True
    privileged = await api.post(
        f"{BASE}/roles",
        headers=headers,
        json={"name": "Privileged", "permission_ids": [await permission_id("shop.product.delete")]},
    )
    role = privileged.json()
    target = (
        await api.post(
            f"{BASE}/users",
            headers=headers,
            json={
                "username": "privileged",
                "password": "long-secure-password",
                "role_ids": [role["id"]],
            },
        )
    ).json()
    headers = await sign_in(api, username="delegate", superuser=False)
    await grant_permissions(
        "delegate",
        "auth.users.read",
        "auth.users.manage",
        "auth.roles.read",
        "auth.roles.manage",
        "auth.roles.manage",
        "shop.product.read",
    )
    assert (
        await api.post(
            f"{BASE}/users",
            headers=headers,
            json={
                "username": "escalated",
                "password": "long-secure-password",
                "role_ids": [role["id"]],
            },
        )
    ).status_code == 403
    assert (
        await api.patch(
            f"{BASE}/users/{target['id']}",
            headers={**headers, "If-Match": target["etag"]},
            json={"password": "reset-secure-password"},
        )
    ).status_code == 403
    assert (
        await api.patch(
            f"{BASE}/roles/{role['id']}",
            headers={**headers, "If-Match": role["etag"]},
            json={"permission_ids": []},
        )
    ).status_code == 403
    assert (
        await api.delete(
            f"{BASE}/roles/{role['id']}", headers={**headers, "If-Match": role["etag"]}
        )
    ).status_code == 403
    assert (
        await api.post(
            f"/admin/roles/{role['id']}",
            data={"csrf_token": headers["X-CSRF-Token"], "name": "Hijacked"},
        )
    ).status_code == 403
    assert (
        await api.post(
            "/admin/users/new",
            data={
                "csrf_token": headers["X-CSRF-Token"],
                "username": "hijacked",
                "password": "long-secure-password",
                "confirm_password": "long-secure-password",
                "role_ids": role["id"],
            },
        )
    ).status_code == 403
    assert (await api.get(f"{BASE}/roles/{role['id']}")).json()["name"] == "Privileged"


async def test_last_superuser_and_pat_cannot_escalate(api: httpx.AsyncClient) -> None:
    headers = await sign_in(api)
    site_for(api).api_auth_enabled = True
    me = (await api.get(f"{BASE}/auth/me")).json()["user"]
    path = f"{BASE}/users/{me['id']}"
    row = (await api.get(path)).json()
    assert (
        await api.patch(
            path, headers={**headers, "If-Match": row["etag"]}, json={"is_active": False}
        )
    ).status_code == 403
    pat = await mint(api, headers, ["auth.user:*", "auth.role:*", "shop.product:read"])
    bearer = {"Authorization": f"Bearer {pat['token']}"}
    assert (
        await api.post(
            f"{BASE}/roles",
            headers=bearer,
            json={
                "name": "Too broad",
                "permission_ids": [await permission_id("shop.product.delete")],
            },
        )
    ).status_code == 403
    assert (
        await api.post(
            f"{BASE}/roles",
            headers=bearer,
            json={"name": "Scoped", "permission_ids": [await permission_id("shop.product.read")]},
        )
    ).status_code == 201
    assert (
        await api.patch(
            path,
            headers={**bearer, "If-Match": row["etag"]},
            json={"password": "reset-secure-password"},
        )
    ).status_code == 403


@pytest.mark.parametrize(
    "scopes,status",
    [
        (["auth.user:write"], 403),
        (["auth.user:delete"], 403),
        (["auth.user:write", "auth.user:delete"], 201),
    ],
)
async def test_pat_needs_all_manage_operations_to_delegate(
    api: httpx.AsyncClient, scopes: list[str], status: int
) -> None:
    headers = await sign_in(api)
    site_for(api).api_auth_enabled = True
    token = await mint(api, headers, ["auth.role:write", *scopes])
    response = await api.post(
        f"{BASE}/roles",
        headers={"Authorization": f"Bearer {token['token']}"},
        json={
            "name": "User administrators",
            "permission_ids": [await permission_id("auth.users.manage")],
        },
    )
    assert response.status_code == status, response.text


async def test_history_scoping_masks_and_revert_hooks(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obox_builder.contrib.history.models import RowChange
    from tests.support.apps.warehouse.models import Item

    headers = await sign_in(api)
    created = await create_item(api, headers)
    pk = created.json()["id"]
    path = f"{ITEMS}/{pk}"
    changed = await api.patch(
        path,
        headers={**headers, "If-Match": created.headers["etag"]},
        json={"fields": {"name": "Changed"}},
    )
    assert changed.status_code == 200
    timeline = (await api.get(f"{path}/history")).json()["entries"]
    assert len(timeline) == 2
    assert timeline[0]["revertable"] is True
    assert (await api.get(f"{BASE}/history?since=2099-01-01T00:00:00Z")).json()["entries"] == []
    assert "secret_token" not in str(timeline)
    calls = []
    admin = site_for(api).get_registry()["warehouse.item"]
    original = admin.save_model

    async def save(ctx: Any, obj: Any, bound: Any, *, change: bool) -> None:
        calls.append(change)
        await original(ctx, obj, bound, change=change)

    monkeypatch.setattr(admin, "save_model", save)
    revert = f"{path}/history/{timeline[0]['id']}/revert"
    assert (await api.post(revert, headers=headers, json={})).status_code == 428
    restored = await api.post(
        revert, headers={**headers, "If-Match": changed.headers["etag"]}, json={"reason": "undo"}
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["fields"]["name"] == created.json()["fields"]["name"]
    assert calls == [True]
    async with session_scope() as db:
        latest = await db.scalar(
            sa.select(RowChange)
            .where(RowChange.model_label == "warehouse.item")
            .order_by(RowChange.id.desc())
        )
        assert latest is not None
        assert latest.source == "api"
        obj = await db.get(Item, int(pk))
        assert obj is not None
    assert (await api.get(f"{BASE}/history?model=warehouse.item&limit=1")).json()[
        "before"
    ] is not None
    monkeypatch.setattr(admin, "get_queryset", lambda ctx: sa.select(Item).where(sa.false()))
    assert (await api.get(f"{path}/history")).status_code == 404
    assert (await api.get(f"{BASE}/history")).json()["entries"] == []
    assert (
        await api.post(revert, headers={**headers, "If-Match": restored.headers["etag"]}, json={})
    ).status_code == 404


async def test_deleted_history_superuser_only_and_restore(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.support.apps.warehouse.models import Shelf

    headers = await sign_in(api)
    base = f"{BASE}/models/warehouse.shelf/records"
    created = await api.post(base, headers=headers, json={"fields": {"code": "Restore"}})
    path = f"{base}/{created.json()['id']}"
    assert (
        await api.delete(path, headers={**headers, "If-Match": created.headers["etag"]})
    ).status_code == 204
    entries = (await api.get(f"{path}/history")).json()["entries"]
    assert entries[0]["op"] == "delete"
    admin = site_for(api).get_registry()["warehouse.shelf"]
    original_save = admin.save_model

    async def save(ctx: Any, obj: Any, bound: Any, *, change: bool) -> None:
        assert ctx.obj is obj
        assert change is False
        await original_save(ctx, obj, bound, change=change)

    monkeypatch.setattr(admin, "save_model", save)
    restored = await api.post(f"{path}/history/{entries[0]['id']}/revert", headers=headers, json={})
    assert restored.status_code == 200, restored.text
    assert restored.json()["fields"]["code"] == "Restore"
    assert (
        await api.delete(path, headers={**headers, "If-Match": restored.headers["etag"]})
    ).status_code == 204
    await sign_in(api, superuser=False, username="reader")
    await grant_permissions(
        "reader",
        "warehouse.shelf.read",
        "history.rowChange.read",
        "history.rowChange.restore",
        "warehouse.shelf.create",
    )
    assert (await api.get(f"{path}/history")).status_code == 404
    assert (await api.get(f"{BASE}/history")).json()["entries"] == []
    async with session_scope() as db:
        assert await db.scalar(sa.select(Shelf)) is None


async def test_comments_threads_etags_scopes_and_hooks(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obox_builder.contrib.comments.models import RowComment
    from tests.support.apps.warehouse.models import Item

    monkeypatch.setattr(Item, "__comments__", True, raising=False)
    headers = await sign_in(api)
    created = await create_item(api, headers)
    path = f"{ITEMS}/{created.json()['id']}/comments"
    root = await api.post(
        path,
        headers=headers,
        json={"body": "<script>unsafe</script>", "idempotency_key": "comment-1"},
    )
    assert root.status_code == 201, root.text
    assert (
        await api.post(
            path,
            headers=headers,
            json={"body": "<script>unsafe</script>", "idempotency_key": "comment-1"},
        )
    ).json() == root.json()
    reply = await api.post(
        path, headers=headers, json={"body": "reply", "parent_id": root.json()["id"]}
    )
    assert reply.status_code == 201, reply.text
    listing = (await api.get(path)).json()
    assert len(listing["threads"][0]["replies"]) == 1
    assert "<script>" not in listing["threads"][0]["root"]["body_html"]
    own_path = f"{path}/{root.json()['id']}"
    assert (await api.patch(own_path, headers=headers, json={"body": "edited"})).status_code == 428
    edited = await api.patch(
        own_path, headers={**headers, "If-Match": root.headers["etag"]}, json={"body": "edited"}
    )
    assert edited.status_code == 200, edited.text
    assert (
        await api.patch(
            own_path, headers={**headers, "If-Match": root.headers["etag"]}, json={"body": "stale"}
        )
    ).status_code == 412
    resolved = await api.post(
        own_path + "/resolve",
        headers={**headers, "If-Match": edited.headers["etag"]},
        json={"resolved": True},
    )
    assert resolved.status_code == 200, resolved.text
    pat = await mint(api, headers, ["warehouse.item:read"])
    bearer = {"Authorization": f"Bearer {pat['token']}"}
    view = (await api.get(path, headers=bearer)).json()["threads"][0]["root"]
    assert not view["can_edit"]
    assert not view["can_delete"]
    assert not view["can_resolve"]
    assert (
        await api.delete(own_path, headers={**bearer, "If-Match": resolved.headers["etag"]})
    ).status_code == 403
    assert (await api.post(path, headers=bearer, json={"body": "denied"})).status_code == 403
    other = await create_item(api, headers, name="Other", sku="other")
    assert (
        await api.patch(
            f"{ITEMS}/{other.json()['id']}/comments/{root.json()['id']}",
            headers={**headers, "If-Match": edited.headers["etag"]},
            json={"body": "leak"},
        )
    ).status_code == 404
    async with session_scope() as db:
        row = await db.get(RowComment, int(root.json()["id"]))
        assert row
        assert row.source == "api"
        assert row.body == "edited"
    admin = site_for(api).get_registry()["warehouse.item"]
    monkeypatch.setattr(admin, "has_comment_view_permission", lambda ctx, obj=None: False)
    assert (await api.get(path)).status_code == 403


async def test_inline_partial_writes_etags_atomicity_and_parent_scope(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obox_builder.contrib.admin import TabularInline
    from sqlalchemy.orm import joinedload
    from tests.admin.test_inlines import create_order, lines_of
    from tests.support.apps.shop.models import Order, OrderLine

    calls = []

    class Lines(TabularInline):
        api_enabled = True
        model = OrderLine
        fields = ("sku", "quantity", "product_id")
        max_num = 3

        def get_queryset(self, ctx: Any) -> Any:
            return super().get_queryset(ctx).options(joinedload(OrderLine.product))

        async def save_model(self, ctx: Any, obj: Any, form: Any, *, change: bool) -> None:
            calls.append(change)
            await super().save_model(ctx, obj, form, change=change)

    admin = site_for(api).get_registry()["shop.order"]
    monkeypatch.setattr(admin, "inlines", (Lines,))
    headers = await sign_in(api)
    pk = await create_order("Inline", "A", "B")
    other = await create_order("Other", "C")
    path = f"{BASE}/models/shop.order/records/{pk}/inlines/order_line"
    schema = (await api.get(f"{BASE}/models/shop.order/schema")).json()
    assert schema["inlines"][0]["name"] == "order_line"
    records = (await api.get(path)).json()["records"]
    first = records[0]
    edit = {"id": first["id"], "etag": first["meta"]["etag"], "fields": {"quantity": 9}}
    result = await api.patch(
        path, headers=headers, json={"records": [edit, {"fields": {"sku": "D", "quantity": 2}}]}
    )
    assert result.status_code == 200, result.text
    assert len(result.json()["records"]) == 2
    assert calls == [True, False]
    assert [(line.sku, line.quantity) for line in await lines_of(pk)] == [
        ("A", 9),
        ("B", 1),
        ("D", 2),
    ]
    assert (await api.patch(path, headers=headers, json={"records": [edit]})).status_code == 412
    fresh = (await api.get(path)).json()["records"][0]
    invalid = [
        {"id": fresh["id"], "etag": fresh["meta"]["etag"], "fields": {"quantity": 3}},
        {"fields": {"sku": "E"}},
    ]
    assert (await api.patch(path, headers=headers, json={"records": invalid})).status_code == 422
    assert (await lines_of(pk))[0].quantity == 9
    forbidden = {"id": str((await lines_of(other))[0].id), "etag": "*", "fields": {"quantity": 3}}
    assert (
        await api.patch(path, headers=headers, json={"records": [forbidden]})
    ).status_code == 404
    bad = {"id": fresh["id"], "etag": fresh["meta"]["etag"], "fields": {"order_id": other}}
    assert (await api.patch(path, headers=headers, json={"records": [bad]})).status_code == 422
    deletion = {"id": fresh["id"], "etag": fresh["meta"]["etag"], "delete": True}
    assert (await api.patch(path, headers=headers, json={"records": [deletion]})).status_code == 200
    assert len(await lines_of(pk)) == 2
    monkeypatch.setattr(admin, "get_queryset", lambda ctx: sa.select(Order).where(sa.false()))
    assert (await api.get(path)).status_code == 404
    assert (
        await api.patch(path, headers=headers, json={"records": [{"fields": {"sku": "No"}}]})
    ).status_code == 404


async def test_custom_api_routes_context_permissions_csrf_and_reserved_paths(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obox_builder.contrib.admin import AdminRoute
    from obox_builder.contrib.admin.context import AdminContext
    from obox_builder.core.application import create_app
    from obox_builder.core.exceptions import ImproperlyConfigured

    admin = site_for(api).get_registry()["warehouse.item"]

    async def report(ctx: AdminContext, count: int = 1) -> dict[str, Any]:
        return {
            "model": ctx.admin.model_label if ctx.admin else "",
            "count": count,
            "api": ctx.extra["api"],
        }

    monkeypatch.setattr(
        admin,
        "get_api_urls",
        lambda: [AdminRoute("report", report, "report", methods=("GET", "POST"))],
    )
    app = create_app()
    app.include_router(site_for(api).get_api_router(), prefix=BASE)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as custom:
        headers = await sign_in(custom)
        path = f"{BASE}/models/warehouse.item/report?count=2"
        assert (await custom.get(path)).json() == {
            "model": "warehouse.item",
            "count": 2,
            "api": True,
        }
        assert (await custom.post(path)).status_code == 403
        assert (await custom.post(path, headers=headers)).status_code == 200
        token = await mint(custom, headers, ["warehouse.item:read"])
        bearer = {"Authorization": f"Bearer {token['token']}"}
        assert (await custom.get(path, headers=bearer)).status_code == 200
        assert (await custom.post(path, headers=bearer)).status_code == 403
    monkeypatch.setattr(admin, "get_api_urls", lambda: [AdminRoute("records/{pk}", report, "bad")])
    with pytest.raises(ImproperlyConfigured):
        site_for(api).get_api_router()


@pytest.mark.parametrize(
    "body",
    [
        {"password": "short"},
        {"role_ids": ["999999"]},
        {"is_superuser": "true"},
        {"role_ids": ["-1"]},
        {"username": "changed"},
    ],
)
async def test_invalid_account_patch_is_atomic(
    api: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    headers = await sign_in(api)
    site_for(api).api_auth_enabled = True
    created = await api.post(
        f"{BASE}/users",
        headers=headers,
        json={"username": "preserved", "password": "long-secure-password"},
    )
    row = created.json()
    path = f"{BASE}/users/{row['id']}"
    response = await api.patch(
        path,
        headers={**headers, "If-Match": row["etag"]},
        json={"email": "must-rollback@test.local", **body},
    )
    assert response.status_code == 422, response.text
    assert (await api.get(path)).json() == row


async def test_history_revert_conflicts_force_and_permissions(api: httpx.AsyncClient) -> None:
    headers = await sign_in(api)
    created = await create_item(api, headers)
    path = f"{ITEMS}/{created.json()['id']}"
    first = await api.patch(
        path,
        headers={**headers, "If-Match": created.headers["etag"]},
        json={"fields": {"quantity": 1}},
    )
    entries = (await api.get(path + "/history")).json()["entries"]
    second = await api.patch(
        path,
        headers={**headers, "If-Match": first.headers["etag"]},
        json={"fields": {"quantity": 2}},
    )
    revert = f"{path}/history/{entries[0]['id']}/revert"
    assert (
        await api.post(revert, headers={**headers, "If-Match": second.headers["etag"]}, json={})
    ).status_code == 409
    assert (
        await api.post(
            revert, headers={**headers, "If-Match": first.headers["etag"]}, json={"force": True}
        )
    ).status_code == 412
    forced = await api.post(
        revert, headers={**headers, "If-Match": second.headers["etag"]}, json={"force": True}
    )
    assert forced.status_code == 200, forced.text
    assert forced.json()["fields"]["quantity"] == 0
    token = await mint(api, headers, ["warehouse.item:read"])
    bearer = {"Authorization": f"Bearer {token['token']}", "If-Match": forced.headers["etag"]}
    assert (await api.post(revert, headers=bearer, json={"force": True})).status_code == 403
    await sign_in(api, username="viewer", superuser=False)
    await grant_permissions("viewer", "warehouse.item.read")
    assert (await api.get(path + "/history")).status_code == 403
    await grant_permissions("viewer", "history.rowChange.read")
    assert (await api.get(path + "/history")).status_code == 200
    assert (
        await api.post(revert, headers={**headers, "If-Match": forced.headers["etag"]}, json={})
    ).status_code == 403


async def test_comment_author_deleted_redaction_validation_and_rate_limit(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obox_builder.conf import settings
    from tests.support.apps.warehouse.models import Item

    monkeypatch.setattr(Item, "__comments__", True, raising=False)
    headers = await sign_in(api)
    record = await create_item(api, headers)
    path = f"{ITEMS}/{record.json()['id']}/comments"
    assert (await api.post(path, headers=headers, json={"body": " "})).status_code == 422
    assert (
        await api.post(path, headers=headers, json={"body": "x", "parent_id": "99999"})
    ).status_code == 422
    root = await api.post(path, headers=headers, json={"body": "Deleted sensitive body"})
    assert root.status_code == 201
    own = f"{path}/{root.json()['id']}"
    assert (
        await api.delete(own, headers={**headers, "If-Match": root.headers["etag"]})
    ).status_code == 204
    other = await sign_in(api, username="commenter", superuser=False)
    await grant_permissions(
        "commenter", "warehouse.item.read", "comments.rowComment.read", "comments.rowComment.create"
    )
    listing = await api.get(path)
    assert listing.status_code == 200, listing.text
    assert "Deleted sensitive body" not in listing.text
    fresh = await api.post(path, headers=other, json={"body": "Own"})
    assert fresh.status_code == 201
    monkeypatch.setattr(settings.load(), "COMMENTS_RATE_LIMIT_PER_MINUTE", 1)
    limited = await api.post(path, headers=other, json={"body": "Throttled"})
    assert limited.status_code == 429, limited.text
    assert int(limited.headers["retry-after"]) > 0


async def test_inline_token_scope_limits_and_hook_failure_rollback(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obox_builder.contrib.admin import TabularInline
    from tests.admin.test_inlines import create_order, lines_of
    from tests.support.apps.shop.models import OrderLine

    class Lines(TabularInline):
        api_enabled = True
        model = OrderLine
        fields = ("sku", "quantity")

    admin = site_for(api).get_registry()["shop.order"]
    monkeypatch.setattr(admin, "inlines", (Lines,))
    headers = await sign_in(api)
    pk = await create_order("Tokens", "A")
    path = f"{BASE}/models/shop.order/records/{pk}/inlines/order_line"
    token = await mint(api, headers, ["shop.order:*", "shop.order_line:read"])
    bearer = {"Authorization": f"Bearer {token['token']}"}
    row = (await api.get(path, headers=bearer)).json()["records"][0]
    body = {"records": [{"id": row["id"], "etag": row["meta"]["etag"], "fields": {"quantity": 8}}]}
    assert (await api.patch(path, headers=bearer, json=body)).status_code == 403
    token = await mint(api, headers, ["shop.order:*", "shop.order_line:*"])
    bearer = {"Authorization": f"Bearer {token['token']}"}

    async def failing(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Private failure")

    monkeypatch.setattr(admin, "save_related", failing)
    refreshed = (await api.get(path, headers=bearer)).json()["records"][0]
    body["records"][0]["etag"] = refreshed["meta"]["etag"]
    failure = await api.patch(path, headers=bearer, json=body)
    assert failure.status_code == 500
    assert "Private failure" not in failure.text
    assert (await lines_of(pk))[0].quantity == 1
    monkeypatch.setattr(Lines, "api_name", "entries")
    assert (await api.get(path)).status_code == 404
    assert (await api.get(path.removesuffix("order_line") + "entries")).status_code == 200
    monkeypatch.setattr(Lines, "api_enabled", False)
    assert (await api.get(path)).status_code == 404


async def test_concurrent_superuser_deactivation_keeps_one_active(api: httpx.AsyncClient) -> None:
    import asyncio

    from obox_builder.conf import settings
    from obox_builder.contrib.auth.models import User
    from tests.admin.api.conftest import site_for

    if not settings.DATABASE_URL.startswith("postgresql"):
        pytest.skip("Concurrent transactions require PostgreSQL.")
    headers = await sign_in(api)
    site_for(api).api_auth_enabled = True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.api_app),  # type: ignore[attr-defined]
        base_url="http://testserver",
    ) as other:
        other_headers = await sign_in(other, username="second-superuser")
        users = (await api.get(f"{BASE}/users")).json()["users"]
        assert len(users) == 2
        responses = await asyncio.gather(
            *[
                client.patch(
                    f"{BASE}/users/{row['id']}",
                    headers={**auth_headers, "If-Match": row["etag"]},
                    json={"is_active": False},
                )
                for client, auth_headers, row in zip(
                    (api, other), (headers, other_headers), users, strict=True
                )
            ]
        )
    assert sorted(r.status_code for r in responses) == [200, 403]
    async with session_scope() as db:
        count = await db.scalar(
            sa.select(sa.func.count())
            .select_from(User)
            .where(User.is_active.is_(True), User.is_superuser.is_(True))
        )
        assert count == 1


async def test_generic_account_admin_cannot_bypass_delegation_policy(
    api: httpx.AsyncClient,
) -> None:
    from obox_builder.contrib.admin import ModelAdmin
    from obox_builder.contrib.auth.models import Role, User
    from obox_builder.core.application import create_app

    headers = await sign_in(api)
    for model in (User, Role):
        site_for(api).register(model, type("ExposedAccount", (ModelAdmin,), {"api_enabled": True}))
    me = (await api.get(f"{BASE}/auth/me")).json()["user"]
    for label in ("auth.user", "auth.role"):
        assert (await api.get(f"{BASE}/models/{label}/records")).status_code == 404
        assert (
            await api.post(f"{BASE}/models/{label}/records", headers=headers, json={"fields": {}})
        ).status_code == 404
    assert "auth.user" not in str((await api.get(f"{BASE}/meta")).json()["models"])
    app = create_app()
    app.include_router(site_for(api).get_router(), prefix="/admin")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", cookies=api.cookies
    ) as html:
        assert (
            await html.post(
                f"/admin/auth/user/{me['id']}",
                data={
                    "csrf_token": headers["X-CSRF-Token"],
                    "is_superuser": "false",
                },
            )
        ).status_code == 403
