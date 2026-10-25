from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.db.session import session_scope
from tests.admin.api.conftest import sign_in, site_for

BASE = "/api/admin/v1"
ITEMS = f"{BASE}/models/warehouse.item/records"
PRODUCTS = f"{BASE}/models/shop.product/records"
ORDERS = f"{BASE}/models/shop.order/records"


async def create_item(
    api: httpx.AsyncClient, headers: dict[str, str], **fields: Any
) -> httpx.Response:
    response = await api.post(
        ITEMS, headers=headers, json={"fields": {"name": "Original", "sku": "sku-1", **fields}}
    )
    assert response.status_code == 201, response.text
    return response


async def test_patch_preserves_values_and_records_history(api: httpx.AsyncClient) -> None:
    from obox_builder.contrib.history.models import RowChange
    from tests.support.apps.warehouse.models import Item

    headers = await sign_in(api)
    created = await create_item(
        api,
        headers,
        notes="Keep me",
        quantity=17,
        price="199.90",
        meta={"nested": {"keep": True, "remove": 1}},
    )
    pk = created.json()["id"]
    assert isinstance(pk, str)
    assert created.json()["fields"]["price"] == "199.90"
    assert not created.headers["etag"].startswith("W/")
    headers["If-Match"] = created.headers["etag"]
    updated = await api.patch(
        f"{ITEMS}/{pk}",
        headers=headers,
        json={"fields": {"name": "Changed", "meta": {"nested": {"remove": None, "new": 2}}}},
    )
    assert updated.status_code == 200, updated.text
    assert updated.headers["etag"] != headers["If-Match"]
    fields = updated.json()["fields"]
    assert (fields["notes"], fields["quantity"], fields["price"]) == ("Keep me", 17, "199.90")
    assert fields["meta"] == {"nested": {"keep": True, "new": 2}}
    async with session_scope() as db:
        obj = await db.get(Item, int(pk))
        assert obj is not None
        assert obj.name == "Changed"
        assert obj.version == 2
        rows = (
            await db.scalars(sa.select(RowChange).where(RowChange.model_label == "warehouse.item"))
        ).all()
        assert len(rows) == 2
        assert all(row.source == "api" for row in rows)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", 99),
        ("version", 0),
        ("created_at", "2026-01-01"),
        ("secret_token", "leak"),
        ("unknown", "value"),
        ("name", None),
    ],
)
async def test_mass_assignment_is_rejected(api: httpx.AsyncClient, field: str, value: Any) -> None:
    headers = await sign_in(api)
    created = await create_item(api, headers)
    headers["If-Match"] = created.headers["etag"]
    response = await api.patch(
        f"{ITEMS}/{created.json()['id']}", headers=headers, json={"fields": {field: value}}
    )
    assert response.status_code == 422, response.text
    assert field in response.json()["errors"]
    assert "secret_token" not in created.json()["fields"]


async def test_etag_required_stale_weak_and_multiple_tags(api: httpx.AsyncClient) -> None:
    headers = await sign_in(api)
    created = await create_item(api, headers)
    url = f"{ITEMS}/{created.json()['id']}"
    body = {"fields": {"name": "Changed"}}
    assert (await api.patch(url, headers=headers, json=body)).status_code == 428
    assert (
        await api.patch(url, headers={**headers, "If-Match": '"old"'}, json=body)
    ).status_code == 412
    assert (
        await api.patch(
            url, headers={**headers, "If-Match": f"W/{created.headers['etag']}"}, json=body
        )
    ).status_code == 412
    match = {**headers, "If-Match": f'"old", {created.headers["etag"]}'}
    assert (await api.patch(url, headers=match, json=body)).status_code == 200
    assert (await api.patch(url, headers=match, json=body)).status_code == 412


async def test_null_and_empty_patch(api: httpx.AsyncClient) -> None:
    headers = await sign_in(api)
    created = await create_item(api, headers, price="12.34")
    url = f"{ITEMS}/{created.json()['id']}"
    unchanged = await api.patch(
        url, headers={**headers, "If-Match": created.headers["etag"]}, json={"fields": {}}
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.headers["etag"] == created.headers["etag"]
    updated = await api.patch(
        url,
        headers={**headers, "If-Match": unchanged.headers["etag"]},
        json={"fields": {"price": None}},
    )
    assert updated.status_code == 200
    assert updated.json()["fields"]["price"] is None


async def test_idempotency_replay_conflict_and_rollback(api: httpx.AsyncClient) -> None:
    from obox_builder.contrib.admin.models import ApiIdempotencyKey
    from tests.support.apps.warehouse.models import Item

    headers = {**await sign_in(api), "Idempotency-Key": "same-key"}
    body = {"fields": {"name": "Once", "sku": "same"}}
    first = await api.post(ITEMS, headers=headers, json=body)
    again = await api.post(ITEMS, headers=headers, json=body)
    assert first.status_code == again.status_code == 201, again.text
    assert first.json() == again.json()
    assert first.headers["etag"] == again.headers["etag"]
    assert first.headers["location"] == again.headers["location"]
    conflict = await api.post(
        ITEMS, headers=headers, json={"fields": {"name": "Other", "sku": "other"}}
    )
    assert conflict.status_code == 422
    failed_headers = {**headers, "Idempotency-Key": "retry-after-error"}
    assert (await api.post(ITEMS, headers=failed_headers, json=body)).status_code == 422
    fixed = await api.post(
        ITEMS, headers=failed_headers, json={"fields": {"name": "Fixed", "sku": "fixed"}}
    )
    assert fixed.status_code == 201, fixed.text
    async with session_scope() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(Item)) == 2
        assert await db.scalar(sa.select(sa.func.count()).select_from(ApiIdempotencyKey)) == 2


async def test_batch_create_and_update_are_atomic(api: httpx.AsyncClient) -> None:
    headers = await sign_in(api)
    invalid = await api.post(
        ITEMS,
        headers=headers,
        json={
            "records": [
                {"fields": {"name": "OK", "sku": "one"}},
                {"fields": {"name": "Bad", "sku": "one"}},
            ]
        },
    )
    assert invalid.status_code == 422, invalid.text
    assert "1" in invalid.json()["errors"]
    assert (await api.get(ITEMS)).json()["records"] == []
    response = await api.post(
        ITEMS,
        headers=headers,
        json={
            "records": [
                {"fields": {"name": "One", "sku": "one"}},
                {"fields": {"name": "Two", "sku": "two"}},
            ]
        },
    )
    assert response.status_code == 201, response.text
    records = response.json()["records"]
    batch = {
        "records": [
            {"id": row["id"], "etag": row["meta"]["etag"], "fields": {"name": f"Edited {index}"}}
            for index, row in enumerate(records)
        ]
    }
    batch["records"][1]["fields"] = {"version": 0}
    assert (await api.patch(ITEMS, headers=headers, json=batch)).status_code == 422
    assert [row["fields"]["name"] for row in (await api.get(ITEMS)).json()["records"]] == [
        "One",
        "Two",
    ]
    batch["records"][1]["fields"] = {"name": "Edited 1"}
    assert (await api.patch(ITEMS, headers=headers, json=batch)).status_code == 200


async def test_delete_and_cascade_confirmation(api: httpx.AsyncClient) -> None:
    from tests.support.apps.shop.models import OrderLine

    headers = await sign_in(api)
    created = await api.post(ORDERS, headers=headers, json={"fields": {"reference": "Order"}})
    assert created.status_code == 201, created.text
    pk = created.json()["id"]
    async with session_scope() as db:
        db.add(OrderLine(order_id=int(pk), sku="line"))
    current = await api.get(f"{ORDERS}/{pk}")
    match = {**headers, "If-Match": current.headers["etag"]}
    preview = await api.get(f"{ORDERS}/{pk}/delete-preview")
    assert preview.status_code == 200
    assert preview.json()["allowed"]
    rejected = await api.delete(f"{ORDERS}/{pk}", headers=match)
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "confirmation_required"
    deleted = await api.delete(
        f"{ORDERS}/{pk}", headers={**match, "X-Delete-Confirmation": preview.json()["confirmation"]}
    )
    assert deleted.status_code == 204, deleted.text
    assert (await api.get(f"{ORDERS}/{pk}")).status_code == 404


async def test_batch_delete_replay_and_missing_row_rolls_back(api: httpx.AsyncClient) -> None:
    headers = await sign_in(api)
    a = (await create_item(api, headers)).json()
    b = (await create_item(api, headers, sku="sku-2")).json()
    body = {"ids": [a["id"], "99999"], "etags": {a["id"]: a["meta"]["etag"]}}
    assert (await api.post(f"{ITEMS}:delete", headers=headers, json=body)).status_code == 404
    assert (await api.get(f"{ITEMS}/{a['id']}")).status_code == 200
    body = {"ids": [a["id"], b["id"]], "etags": {row["id"]: row["meta"]["etag"] for row in (a, b)}}
    headers["Idempotency-Key"] = "delete-batch"
    for _ in range(2):
        response = await api.post(f"{ITEMS}:delete", headers=headers, json=body)
        assert response.status_code == 204, response.text


async def test_query_filter_projection_and_pagination(api: httpx.AsyncClient) -> None:
    headers = await sign_in(api)
    await create_item(api, headers, name="Needle", quantity=3)
    await create_item(api, headers, sku="sku-2", name="Needle too", quantity=8)
    response = await api.get(
        ITEMS,
        params={"q": "Needle", "size": 1, "sort": "quantity", "fields": "name", "total": "exact"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["page"] == {
        "number": 1,
        "size": 1,
        "has_next": True,
        "total": 2,
        "total_capped": False,
    }
    assert response.json()["records"][0]["fields"] == {"name": "Needle"}
    assert 'rel="next"' in response.headers["link"]
    query = await api.post(
        f"{ITEMS}:query",
        headers=headers,
        json={
            "filter": {
                "conjunction": "and",
                "children": [{"field": "quantity", "op": "gte", "value": 5}],
            },
            "fields": ["name"],
            "total": "capped",
        },
    )
    assert query.status_code == 200, query.text
    assert query.json()["records"][0]["fields"] == {"name": "Needle too"}
    assert query.json()["page"]["total"] == 1
    for params in ({"sort": "secret_token"}, {"fields": "secret_token"}, {"total": "invalid"}):
        assert (await api.get(ITEMS, params=params)).status_code == 400


async def test_links_and_options_are_scoped(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.support.apps.warehouse.models import Shelf, Tag

    headers = await sign_in(api)
    async with session_scope() as db:
        shelf = Shelf(code="Visible shelf")
        tags = [Tag(label="Visible"), Tag(label="Hidden")]
        db.add_all([shelf, *tags])
        await db.flush()
        shelf_id, tag_ids = str(shelf.id), [str(tag.id) for tag in tags]
    created = await create_item(api, headers, shelf_id=shelf_id, tags=tag_ids)
    listing = (await api.get(ITEMS)).json()
    assert listing["included"]["shelf_id"][shelf_id]["label"] == "Visible shelf"
    assert created.json()["fields"]["tags"] == tag_ids
    tag_admin = site_for(api).get_model_admin_for(Tag)
    monkeypatch.setattr(
        tag_admin, "get_queryset", lambda ctx: sa.select(Tag).where(Tag.label != "Hidden")
    )
    listing = (await api.get(ITEMS)).json()
    assert listing["records"][0]["fields"]["tags"] == tag_ids[:1]
    assert set(listing["included"]["tags"]) == set(tag_ids[:1])
    options = await api.get(f"{BASE}/models/warehouse.item/options/tags")
    assert options.status_code == 200
    assert len(options.json()["options"]) == 1
    single = await api.get(f"{BASE}/models/shop.product/options/status", params={"q": "live"})
    assert single.json()["options"] == [{"id": "live", "label": "Live"}]


async def test_concurrent_idempotency_one_record(api: httpx.AsyncClient) -> None:
    from obox_builder.conf import settings

    if settings.DATABASE_URL.startswith("sqlite"):
        pytest.skip("Concurrent request transactions require separate PostgreSQL connections.")
    headers = {**await sign_in(api), "Idempotency-Key": "concurrent-key"}
    responses = await asyncio.gather(
        *(
            api.post(ITEMS, headers=headers, json={"fields": {"name": "Once", "sku": "once"}})
            for _ in range(2)
        )
    )
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    assert len((await api.get(ITEMS)).json()["records"]) == 1
