from __future__ import annotations

from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.contrib.admin import ModelAdmin, site
from obox_builder.db.session import session_scope
from sqlalchemy.orm import selectinload

from tests.admin.test_permissions import signed_in
from tests.support.apps.shop.models import Label, Order

URL = "/admin/shop/order"


async def labels_of(pk: int) -> list[str]:
    async with session_scope() as db:
        order = await db.scalar(
            sa.select(Order).where(Order.id == pk).options(selectinload(Order.labels))
        )
        assert order is not None
        return sorted(label.id for label in order.labels)


async def setup_labels() -> None:
    async with session_scope() as db:
        db.add_all([Label(id="red", title="Red label"), Label(id="blue", title="Blue label")])


async def test_many_to_many_create_update_clear_and_readonly(
    relation_client: httpx.AsyncClient,
) -> None:
    client = relation_client
    token = await signed_in(client)
    await setup_labels()
    created = await client.post(
        f"{URL}/add",
        data={
            "csrf_token": token,
            "reference": "MULTI-1",
            "labels": ["", "red", "blue", "red"],
        },
    )
    assert created.status_code == 303
    async with session_scope() as db:
        pk = await db.scalar(sa.select(Order.id).where(Order.reference == "MULTI-1"))
    assert pk is not None
    assert await labels_of(pk) == ["blue", "red"]
    form = await client.get(f"{URL}/{pk}")
    assert 'name="labels"' in form.text
    assert "multiple" in form.text
    assert 'value="red" selected>Red label' in form.text
    assert 'value="blue" selected>Blue label' in form.text
    changed = await client.post(
        f"{URL}/{pk}", data={"csrf_token": token, "reference": "MULTI-2", "labels": ["", "blue"]}
    )
    assert changed.status_code == 303
    assert await labels_of(pk) == ["blue"]
    cleared = await client.post(
        f"{URL}/{pk}", data={"csrf_token": token, "reference": "MULTI-2", "labels": ""}
    )
    assert cleared.status_code == 303
    assert await labels_of(pk) == []


@pytest.mark.parametrize("restriction", ["missing", "queryset", "object", "model"])
async def test_many_to_many_rejects_invalid_selections_atomically(
    relation_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    restriction: str,
) -> None:
    client = relation_client
    token = await signed_in(client)
    await setup_labels()
    async with session_scope() as db:
        order = Order(reference="Original", labels=[await db.get(Label, "red")])
        db.add(order)
        await db.flush()
        pk = order.id
    admin = site.get_model_admin_for(Label)
    assert admin is not None
    if restriction == "queryset":
        monkeypatch.setattr(
            admin,
            "get_queryset",
            lambda ctx: ModelAdmin.get_queryset(admin, ctx).where(Label.id != "blue"),
        )
    elif restriction in {"object", "model"}:
        monkeypatch.setattr(
            admin,
            "has_view_permission",
            lambda ctx, obj=None: restriction != "model" and (obj is None or obj.id != "blue"),
        )
    response = await client.post(
        f"{URL}/{pk}",
        data={
            "csrf_token": token,
            "reference": "Changed",
            "labels": ["red", "missing" if restriction == "missing" else "blue"],
        },
    )
    assert response.status_code == 200
    assert "Select a valid choice." in response.text
    assert await labels_of(pk) == ["red"]
    async with session_scope() as db:
        refreshed = await db.get(Order, pk)
        assert refreshed is not None
        assert refreshed.reference == "Original"


async def test_many_to_many_rechecks_policy_inside_transaction(
    relation_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = relation_client
    token = await signed_in(client)
    await setup_labels()
    target = site.get_model_admin_for(Label)
    parent = site.get_model_admin_for(Order)
    assert target is not None
    assert parent is not None
    allowed = True
    monkeypatch.setattr(target, "has_view_permission", lambda *args: allowed)

    async def save(ctx: Any, obj: Any, form: Any, *, change: bool) -> None:
        nonlocal allowed
        await ModelAdmin.save_model(parent, ctx, obj, form, change=change)
        allowed = False

    monkeypatch.setattr(parent, "save_model", save)
    response = await client.post(
        f"{URL}/add", data={"csrf_token": token, "reference": "Rollback", "labels": "red"}
    )
    assert response.status_code == 403
    async with session_scope() as db:
        assert await db.scalar(sa.select(Order).where(Order.reference == "Rollback")) is None


async def test_repeated_csrf_token_is_rejected(relation_client: httpx.AsyncClient) -> None:
    client = relation_client
    token = await signed_in(client)
    response = await client.post(
        f"{URL}/add", data={"csrf_token": [token, token], "reference": "Forged"}
    )
    assert response.status_code == 403
    async with session_scope() as db:
        assert await db.scalar(sa.select(Order).where(Order.reference == "Forged")) is None
