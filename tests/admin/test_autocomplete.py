from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.contrib.admin import ModelAdmin
from obox_builder.db.session import session_scope

from tests.admin.conftest import grant_permissions, login_session, make_user
from tests.admin.test_model_views import make_category, make_product
from tests.admin.test_permissions import signed_in
from tests.support.apps.shop.admin import CategoryAdmin, ProductAdmin
from tests.support.apps.shop.models import Asset, Category, Order, OrderLine

URL = "/admin/shop/product/autocomplete/category_id"


@pytest.fixture(autouse=True)
def configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ProductAdmin, "autocomplete_fields", ("category_id",))
    monkeypatch.setattr(CategoryAdmin, "search_fields", ("name",))


async def test_autocomplete_pages_search_and_selected_values(client: httpx.AsyncClient) -> None:
    await signed_in(client)
    async with session_scope() as db:
        db.add_all(Category(name=f"Category {index:02}") for index in range(45))
    first = await client.get(URL)
    assert len(first.json()["results"]) == 20
    assert first.json()["pagination"]["more"] is True
    second = await client.get(URL, params={"page": 2})
    third = await client.get(URL, params={"page": 3})
    assert not {item["id"] for item in first.json()["results"]} & {
        item["id"] for item in second.json()["results"]
    }
    assert len(third.json()["results"]) == 5
    assert third.json()["pagination"]["more"] is False
    category = await make_category("Điện thoại special%_")
    result = await client.get(URL, params={"q": "dien thoai special%_"})
    assert result.json()["results"] == [{"id": str(category), "text": "Điện thoại special%_"}]
    pk = await make_product("Original", category_id=category)
    form = await client.get(f"/admin/shop/product/{pk}")
    assert f'<option value="{category}" selected>Điện thoại special%_</option>' in form.text
    assert "Category 00" not in form.text
    assert "data-relation-picker" in form.text


@pytest.mark.parametrize("restriction", ["source", "target", "queryset", "object", "readonly"])
async def test_autocomplete_applies_source_and_target_policies(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    restriction: str,
) -> None:
    await signed_in(client)
    await make_category("Private")
    if restriction == "source":
        monkeypatch.setattr(ProductAdmin, "has_add_permission", lambda *args: False)
    elif restriction == "target":
        monkeypatch.setattr(CategoryAdmin, "has_view_permission", lambda *args: False)
    elif restriction == "queryset":
        monkeypatch.setattr(
            CategoryAdmin,
            "get_queryset",
            lambda self, ctx: ModelAdmin.get_queryset(self, ctx).where(sa.false()),
        )
    elif restriction == "object":
        monkeypatch.setattr(
            CategoryAdmin, "has_view_permission", lambda self, ctx, obj=None: obj is None
        )
    else:
        monkeypatch.setattr(
            ProductAdmin,
            "get_form_fields",
            lambda self, ctx: [replace(spec, readonly=True) for spec in self.get_form_specs()],
        )
    response = await client.get(URL)
    assert response.status_code == (
        403 if restriction in {"source", "target"} else 400 if restriction == "readonly" else 200
    )
    assert "Private" not in response.text
    if restriction in {"queryset", "object"}:
        assert response.json()["results"] == []


@pytest.mark.parametrize(
    "params",
    [{"page": "0"}, {"page": "1001"}, {"page": "bad"}, {"object": "missing"}, {"inline": "999"}],
)
async def test_autocomplete_rejects_invalid_input(
    client: httpx.AsyncClient, params: dict[str, str]
) -> None:
    await signed_in(client)
    assert (await client.get(URL, params=params)).status_code == 400
    assert (await client.get(URL.replace("category_id", "name"))).status_code == 400


async def test_autocomplete_requires_target_view_permission(client: httpx.AsyncClient) -> None:
    await make_user("writer")
    await grant_permissions("writer", "shop.add_product")
    await login_session(client, "writer")
    assert (await client.get(URL)).status_code == 403
    await grant_permissions("writer", "shop.view_category")
    assert (await client.get(URL)).status_code == 200


async def test_uuid_link_round_trip_and_search(
    relation_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obox_builder.contrib.admin import site

    client = relation_client
    token = await signed_in(client)
    asset_id = uuid4()
    async with session_scope() as db:
        db.add(Asset(id=asset_id, name="UUID asset"))
        order = Order(reference="UUID order")
        db.add(order)
        await db.flush()
        order_id = order.id
    admin = site.get_model_admin_for(OrderLine)
    assert admin is not None
    monkeypatch.setattr(admin, "autocomplete_fields", ("asset_id",))
    result = await client.get("/admin/shop/order_line/autocomplete/asset_id", params={"q": "UUID"})
    assert result.json()["results"] == [{"id": str(asset_id), "text": "UUID asset"}]
    response = await client.post(
        "/admin/shop/order_line/add",
        data={
            "csrf_token": token,
            "order_id": str(order_id),
            "sku": "SKU-UUID",
            "quantity": "1",
            "product_id": "",
            "asset_id": str(asset_id),
        },
    )
    assert response.status_code == 303
    async with session_scope() as db:
        row = await db.scalar(sa.select(OrderLine).where(OrderLine.sku == "SKU-UUID"))
        assert row is not None
        assert row.asset_id == asset_id
