from __future__ import annotations

import sqlalchemy as sa
from obox_builder.db import get_sessionmaker
from tests.api.conftest import create_item, grant, sign_in


async def test_bulk_operations_preflight_all_rows_and_preserve_atomicity(feature_api):
    api = feature_api
    headers = await sign_in(api)
    path = "/api/warehouse/item"
    duplicate = await api.post(
        path + "/actions/bulkCreate",
        headers=headers,
        json={"items": [{"name": "One", "sku": "same"}, {"name": "Two", "sku": "same"}]},
    )
    assert duplicate.status_code == 422, duplicate.text
    assert (await api.get(path)).json()["items"] == []
    created = await api.post(
        path + "/actions/bulkCreate",
        headers=headers,
        json={"items": [{"name": "One", "sku": "one"}, {"name": "Two", "sku": "two"}]},
    )
    assert created.status_code == 200, created.text
    items = created.json()["items"]
    etags = {row["id"]: (await api.get(path + "/" + row["id"])).headers["etag"] for row in items}
    changes = [{"id": row["id"], "data": {"quantity": 5}} for row in items]
    body = {"ids": list(etags), "data": {"changes": changes}, "etags": etags}
    changes[1]["data"] = {"version": 0}
    invalid = await api.post(path + "/actions/bulkUpdate", headers=headers, json=body)
    assert invalid.status_code == 422, invalid.text
    assert [row["quantity"] for row in (await api.get(path)).json()["items"]] == [0, 0]
    changes[1]["data"] = {"quantity": 7}
    body["etags"] = {**etags, items[1]["id"]: '"stale"'}
    stale = await api.post(path + "/actions/bulkUpdate", headers=headers, json=body)
    assert stale.status_code == 412, stale.text
    body["etags"] = etags
    updated = await api.post(path + "/actions/bulkUpdate", headers=headers, json=body)
    assert updated.status_code == 200, updated.text
    assert [row["quantity"] for row in updated.json()["items"]] == [5, 7]


async def test_related_collections_use_typed_child_schemas_and_atomic_writes(feature_api):
    api = feature_api
    headers = await sign_in(api)
    created = await api.post("/api/shop/order", headers=headers, json={"reference": "Order"})
    assert created.status_code == 201, created.text
    path = "/api/shop/order/" + created.json()["id"] + "/actions/"
    listed = await api.post(path + "listRelated", headers=headers, json={"relation": "lines"})
    assert listed.status_code == 200, listed.text
    current = listed.json()
    changes = [{"action": "create", "data": {"sku": "A", "quantity": 2}}]
    response = await api.post(
        path + "editRelated",
        headers=headers,
        json={"relation": "lines", "revision": current["revision"], "changes": changes},
    )
    assert response.status_code == 200, response.text
    current = response.json()
    assert current["items"][0]["sku"] == "A"
    key = current["items"][0]["id"]
    changes = [
        {"action": "update", "id": key, "data": {"quantity": 9}},
        {"action": "create", "data": {"sku": "B", "quantity": "invalid"}},
    ]
    invalid = await api.post(
        path + "editRelated",
        headers=headers,
        json={"relation": "lines", "revision": current["revision"], "changes": changes},
    )
    assert invalid.status_code == 422, invalid.text
    assert (await api.get("/api/shop/orderLine/" + key)).json()["quantity"] == 2
    changes.pop()
    updated = await api.post(
        path + "editRelated",
        headers=headers,
        json={"relation": "lines", "revision": current["revision"], "changes": changes},
    )
    assert updated.status_code == 200, updated.text
    stale = await api.post(
        path + "editRelated",
        headers=headers,
        json={"relation": "lines", "revision": current["revision"], "changes": changes},
    )
    assert stale.status_code == 409
    moved = await api.post(
        path + "editRelated",
        headers=headers,
        json={
            "relation": "lines",
            "revision": updated.json()["revision"],
            "changes": [{"action": "update", "id": key, "data": {"order_id": "999"}}],
        },
    )
    assert moved.status_code == 422


async def test_comments_share_policy_versioning_and_tombstone_redaction(feature_api):
    api = feature_api
    headers = await sign_in(api)
    ticket = await api.post("/api/commentsdemo/ticket", headers=headers, json={"title": "Ticket"})
    assert ticket.status_code == 201, ticket.text
    path = "/api/commentsdemo/ticket/" + ticket.json()["id"] + "/actions/"
    added = await api.post(path + "addComment", headers=headers, json={"body": "Original body"})
    assert added.status_code == 200, added.text
    row = added.json()
    edited = await api.post(
        path + "editComment",
        headers=headers,
        json={"comment_id": row["id"], "version": row["version"], "body": "Updated body"},
    )
    assert edited.status_code == 200, edited.text
    stale = await api.post(
        path + "deleteComment",
        headers=headers,
        json={"comment_id": row["id"], "version": row["version"]},
    )
    assert stale.status_code == 409, stale.text
    deleted = await api.post(
        path + "deleteComment",
        headers=headers,
        json={"comment_id": row["id"], "version": edited.json()["version"]},
    )
    assert deleted.status_code == 200, deleted.text
    await sign_in(api, username="reader", superuser=False)
    await grant("reader", "commentsdemo.ticket.read", "comments.rowComment.read")
    listing = await api.post(
        path + "listComments",
        headers={"X-CSRF-Token": (await api.get("/api/auth/me")).json()["csrf_token"]},
        json={},
    )
    assert listing.status_code == 200, listing.text
    assert "Updated body" not in listing.text


async def test_saved_views_stay_private_and_use_the_shared_query_contract(feature_api):
    api = feature_api
    headers = await sign_in(api)
    path = "/api/warehouse/item/actions/"
    saved = await api.post(
        path + "saveView",
        headers=headers,
        json={"name": "In stock", "config": {"sort": "-quantity", "q": "stock"}},
    )
    assert saved.status_code == 200, saved.text
    identity = saved.json()["id"]
    assert (await api.post(path + "getView", headers=headers, json={"id": identity})).json()[
        "name"
    ] == "In stock"
    invalid = await api.post(
        path + "saveView",
        headers=headers,
        json={"name": "Secret", "config": {"sort": "secret_token"}},
    )
    assert invalid.status_code == 400, invalid.text
    other = await sign_in(api, username="other")
    assert (
        await api.post(path + "getView", headers=other, json={"id": identity})
    ).status_code == 404
    assert (await api.post(path + "listViews", headers=other, json={})).json()["items"] == []


async def test_history_restoration_and_redaction_work_without_admin(feature_api):
    from obox_builder.contrib.history.models import RowChange

    api = feature_api
    headers = await sign_in(api)
    created = await create_item(api, headers)
    path = "/api/warehouse/item/" + created.json()["id"]
    changed = await api.patch(
        path, headers={**headers, "If-Match": created.headers["etag"]}, json={"quantity": 5}
    )
    assert changed.status_code == 200, changed.text
    async with get_sessionmaker()() as db:
        entry = await db.scalar(
            sa.select(RowChange).where(
                RowChange.model_label == "warehouse.item", RowChange.op == "update"
            )
        )
        assert entry is not None
        identity = str(entry.id)
    restored = await api.post(
        "/api/history/rowChange/" + identity + "/actions/restore", headers=headers, json={}
    )
    assert restored.status_code == 200, restored.text
    assert (await api.get(path)).json()["quantity"] == 0
    history = (await api.get("/api/history/rowChange/" + identity)).json()
    assert history["object_repr"] == "#" + created.json()["id"]
    assert "secret_token" not in str(history["changes"])
