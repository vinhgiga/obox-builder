from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.conf import settings
from obox_builder.contrib.admin import ModelAdmin
from obox_builder.core.utils.time import utcnow
from obox_builder.db import get_engine
from obox_builder.db.session import session_scope
from pydantic import create_model, model_validator
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.admin.api.conftest import sign_in, site_for
from tests.admin.api.test_records import BASE, ITEMS, create_item


async def test_list_fifty_rows_two_links_uses_at_most_four_queries(api: httpx.AsyncClient) -> None:
    from tests.support.apps.warehouse.models import Item, Shelf, Tag

    await sign_in(api)
    async with session_scope() as db:
        shelf = Shelf(code="shelf")
        tag = Tag(label="tag")
        db.add_all(
            [
                Item(name=f"Item {index}", sku=str(index), shelf=shelf, tags=[tag])
                for index in range(50)
            ]
        )
    statements: list[str] = []

    def count(
        _conn: Any, _cursor: Any, statement: str, _params: Any, _context: Any, _many: Any
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT") and "warehouse_" in statement:
            statements.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        response = await api.get(ITEMS, params={"size": 50})
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert response.status_code == 200, response.text
    assert len(response.json()["records"]) == 50
    assert len(statements) <= 4, statements


async def test_partial_update_keeps_custom_form_validation_and_hooks(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.support.apps.warehouse.models import Item

    headers = await sign_in(api)
    created = await create_item(api, headers, quantity=7)
    admin = site_for(api).get_model_admin_for(Item)
    calls: list[tuple[bool, int]] = []

    def get_form(ctx: Any, *, change: bool = False) -> Any:
        def check(form: Any) -> Any:
            if form.quantity < 0:
                raise ValueError("Quantity must be positive.")
            return form

        return create_model(
            "CustomItemForm",
            __base__=ModelAdmin.get_form(admin, ctx, change=change),
            __validators__={"check": cast(Any, model_validator(mode="after")(check))},
        )

    async def save(ctx: Any, obj: Any, form: Any, *, change: bool) -> None:
        calls.append((change, form.parsed.quantity))
        await ModelAdmin.save_model(admin, ctx, obj, form, change=change)

    monkeypatch.setattr(admin, "get_form", get_form)
    monkeypatch.setattr(admin, "save_model", save)
    headers["If-Match"] = created.headers["etag"]
    url = f"{ITEMS}/{created.json()['id']}"
    invalid = await api.patch(url, headers=headers, json={"fields": {"quantity": -1}})
    assert invalid.status_code == 422
    assert calls == []
    updated = await api.patch(url, headers=headers, json={"fields": {"name": "Still valid"}})
    assert updated.status_code == 200, updated.text
    assert calls == [(True, 7)]


async def test_dynamic_fields_are_scoped_per_row(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.support.apps.warehouse.models import Item

    headers = await sign_in(api)
    await create_item(api, headers, name="Public", notes="Public note")
    await create_item(api, headers, name="Private", sku="private", notes="Hidden note")
    admin = site_for(api).get_model_admin_for(Item)

    def fields(ctx: Any) -> Any:
        return [
            spec
            for spec in ModelAdmin.get_form_fields(admin, ctx)
            if ctx.obj is None or ctx.obj.name != "Private" or spec.name != "notes"
        ]

    monkeypatch.setattr(admin, "get_form_fields", fields)
    records = (await api.get(ITEMS)).json()["records"]
    assert records[0]["fields"]["notes"] == "Public note"
    assert "notes" not in records[1]["fields"]


async def test_real_user_version_is_readonly_and_password_is_secret(api: httpx.AsyncClient) -> None:
    from obox_builder.contrib.auth.models import User

    headers = await sign_in(api)
    site_for(api).register(User, type("ApiUserAdmin", (ModelAdmin,), {"api_enabled": True}))
    url = f"{BASE}/models/auth.user/records"
    response = await api.get(url)
    assert response.status_code == 200, response.text
    record = response.json()["records"][0]
    assert "password_hash" not in record["fields"]
    invalid = await api.patch(
        f"{url}/{record['id']}",
        headers={**headers, "If-Match": record["meta"]["etag"]},
        json={"fields": {"version": 0}},
    )
    assert invalid.status_code == 422


async def test_idempotency_expiration_owner_scope_and_purge(api: httpx.AsyncClient) -> None:
    from obox_builder.contrib.admin.management.commands.admin_api_purge import handle
    from obox_builder.contrib.admin.models import ApiIdempotencyKey

    headers = {**await sign_in(api), "Idempotency-Key": "expires"}
    await create_item(api, headers)
    async with session_scope() as db:
        key = await db.scalar(sa.select(ApiIdempotencyKey))
        assert key is not None
        key.expires_at = utcnow() - timedelta(seconds=1)
    reused = await create_item(api, headers, sku="new")
    assert reused.json()["id"] != "1"
    other = {**await sign_in(api, username="another"), "Idempotency-Key": "expires"}
    assert (await create_item(api, other, sku="other")).status_code == 201
    async with session_scope() as db:
        keys = (await db.scalars(sa.select(ApiIdempotencyKey).order_by(ApiIdempotencyKey.id))).all()
        assert len(keys) == 2
        keys[0].expires_at = utcnow() - timedelta(seconds=1)
    await handle.func(batch_size=1)
    async with session_scope() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(ApiIdempotencyKey)) == 1


async def test_commit_failure_is_conflict_and_rolls_back(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.support.apps.warehouse.models import Item

    headers = await sign_in(api)
    commit = AsyncSession.commit

    async def fail(db: AsyncSession) -> None:
        raise IntegrityError("COMMIT", {}, RuntimeError("private database detail"))

    monkeypatch.setattr(AsyncSession, "commit", fail)
    response = await api.post(
        ITEMS,
        headers={**headers, "Idempotency-Key": "failed-commit"},
        json={"fields": {"name": "Fail", "sku": "fail"}},
    )
    assert response.status_code == 409
    assert "private database detail" not in response.text
    monkeypatch.setattr(AsyncSession, "commit", commit)
    async with session_scope() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(Item)) == 0


async def test_json_string_and_invalid_boolean(api: httpx.AsyncClient) -> None:
    headers = await sign_in(api)
    created = await create_item(api, headers, meta="literal JSON string")
    assert created.json()["fields"]["meta"] == "literal JSON string"
    invalid = await api.post(
        ITEMS,
        headers=headers,
        json={"fields": {"name": "Bad", "sku": "bad", "is_active": "nonsense"}},
    )
    assert invalid.status_code == 422


async def test_partial_composite_uniqueness_and_large_ids(api: httpx.AsyncClient) -> None:
    from tests.support.apps.shop.models import Order, OrderLine
    from tests.support.apps.warehouse.models import Item

    headers = await sign_in(api)
    site_for(api).register(OrderLine, type("ApiLineAdmin", (ModelAdmin,), {"api_enabled": True}))
    async with session_scope() as db:
        order = Order(reference="unique-order")
        db.add(order)
        await db.flush()
        first = OrderLine(order_id=order.id, sku="first")
        db.add_all(
            [
                first,
                OrderLine(order_id=order.id, sku="second"),
                Item(id=9007199254740993, name="Large id", sku="large"),
            ]
        )
        await db.flush()
        pk = str(first.id)
    url = f"{BASE}/models/shop.order_line/records/{pk}"
    current = await api.get(url)
    assert current.status_code == 200, current.text
    invalid = await api.patch(
        url,
        headers={**headers, "If-Match": current.headers["etag"]},
        json={"fields": {"sku": "second"}},
    )
    assert invalid.status_code == 422, invalid.text
    assert "sku" in invalid.json()["errors"]
    large = await api.get(f"{ITEMS}/9007199254740993")
    assert large.json()["id"] == "9007199254740993"
    assert large.json()["fields"]["id"] == "9007199254740993"


async def test_idempotency_replay_respects_revoked_relation_visibility(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.support.apps.warehouse.models import Shelf

    headers = {**await sign_in(api), "Idempotency-Key": "scoped-replay"}
    async with session_scope() as db:
        shelf = Shelf(code="restricted")
        db.add(shelf)
        await db.flush()
        shelf_id = str(shelf.id)
    await create_item(api, headers, shelf_id=shelf_id)
    admin = site_for(api).get_model_admin_for(Shelf)
    monkeypatch.setattr(admin, "get_queryset", lambda ctx: sa.select(Shelf).where(sa.false()))
    replay = await api.post(
        ITEMS,
        headers=headers,
        json={"fields": {"name": "Original", "sku": "sku-1", "shelf_id": shelf_id}},
    )
    assert replay.status_code == 404


async def test_api_settings_checks(admin_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from obox_builder.contrib.admin.checks import check_admin_api

    assert settings._wrapped is not None
    monkeypatch.setattr(settings._wrapped, "ADMIN_API_TRUSTED_ORIGINS", ["*"])
    assert [message.id for message in check_admin_api()] == ["obox.admin.E501"]
