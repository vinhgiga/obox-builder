from __future__ import annotations

from dataclasses import replace
from typing import Any

import httpx
import pytest
from obox_builder.contrib.admin import ModelAdmin, action
from obox_builder.db.session import session_scope

from tests.admin.conftest import csrf_for, grant_permissions, login_session, make_user
from tests.admin.test_model_views import (
    LIST_URL,
    get_product,
    make_category,
    make_product,
    product_form,
)
from tests.support.apps.shop.admin import CategoryAdmin, ProductAdmin
from tests.support.apps.shop.models import Category, Product


async def signed_in(client: httpx.AsyncClient) -> str:
    await make_user("operator", is_superuser=True)
    await login_session(client, "operator")
    return await csrf_for("operator")


async def product(pk: int) -> Product:
    obj = await get_product(pk)
    assert obj is not None
    return obj


@pytest.mark.parametrize(
    ("permission", "method", "suffix"),
    [
        ("view", "GET", ""),
        ("add", "GET", "/add"),
        ("add", "POST", "/add"),
        ("change", "POST", "/{pk}"),
        ("delete", "GET", "/{pk}/delete"),
        ("delete", "POST", "/{pk}/delete"),
        ("change", "POST", "/inline"),
    ],
)
async def test_permission_hooks_are_enforced_on_server(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    permission: str,
    method: str,
    suffix: str,
) -> None:
    token = await signed_in(client)
    pk = await make_product("Original")
    monkeypatch.setattr(ProductAdmin, f"has_{permission}_permission", lambda *args: False)
    response = await client.request(
        method,
        LIST_URL + suffix.format(pk=pk),
        data={"csrf_token": token, **product_form(name="Forged")},
    )
    assert response.status_code == 403
    assert (await product(pk)).name == "Original"


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("GET", "/{pk}"),
        ("POST", "/{pk}"),
        ("GET", "/{pk}/delete"),
        ("POST", "/{pk}/delete"),
        ("POST", "/inline"),
        ("POST", "/actions"),
    ],
)
async def test_queryset_cannot_be_bypassed_by_posting_ids(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    suffix: str,
) -> None:
    token = await signed_in(client)
    visible = await make_product("Visible")
    hidden = await make_product("Hidden", is_active=False)
    monkeypatch.setattr(ProductAdmin, "list_editable", ("name",))
    monkeypatch.setattr(
        ProductAdmin,
        "get_queryset",
        lambda self, ctx: ModelAdmin.get_queryset(self, ctx).where(Product.is_active),
    )
    response = await client.request(
        method,
        LIST_URL + suffix.format(pk=hidden),
        data={
            "csrf_token": token,
            **product_form(name="Forged"),
            f"edit:{visible}:name": "Changed",
            f"edit:{hidden}:name": "Forged",
            "action": "delete_selected",
            "_selected_action": [str(visible), str(hidden)],
        },
    )
    assert response.status_code == 404
    assert "Hidden" not in response.text
    assert (await product(hidden)).name == "Hidden"
    assert (await product(visible)).name == "Visible"


@pytest.mark.parametrize("operation", ["change", "inline", "delete", "bulk", "all", "custom"])
async def test_object_permission_denial_never_partially_mutates(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    token = await signed_in(client)
    visible = await make_product("Editable")
    locked = await make_product("Locked")
    monkeypatch.setattr(ProductAdmin, "list_editable", ("name",))
    for permission in ("change", "delete"):
        monkeypatch.setattr(
            ProductAdmin,
            f"has_{permission}_permission",
            lambda self, ctx, obj=None: obj is None or obj.name != "Locked",
        )
    calls: list[bool] = []

    @action(permissions=("change",))
    async def publish(admin: Any, ctx: Any, queryset: Any) -> None:
        calls.append(True)

    monkeypatch.setattr(ProductAdmin, "actions", (publish,))
    suffix = {"change": f"/{locked}", "delete": f"/{locked}/delete", "inline": "/inline"}.get(
        operation, "/actions"
    )
    response = await client.post(
        LIST_URL + suffix,
        data={
            "csrf_token": token,
            **product_form(name="Forged"),
            f"edit:{visible}:name": "Changed",
            f"edit:{locked}:name": "Forged",
            "action": "publish" if operation == "custom" else "delete_selected",
            "select_across": "1" if operation == "all" else "0",
            "_selected_action": [str(visible), str(locked)],
        },
    )
    assert response.status_code == 403
    assert (await product(visible)).name == "Editable"
    assert (await product(locked)).name == "Locked"
    assert not calls


async def test_object_permissions_control_list_and_readonly_detail(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await signed_in(client)
    locked = await make_product("Locked")
    denied = await make_product("Invisible")
    monkeypatch.setattr(ProductAdmin, "list_editable", ("name",))
    monkeypatch.setattr(
        ProductAdmin,
        "has_view_permission",
        lambda self, ctx, obj=None: obj is None or obj.name != "Invisible",
    )
    for permission in ("change", "delete"):
        monkeypatch.setattr(ProductAdmin, f"has_{permission}_permission", lambda *args: False)
    listing = await client.get(LIST_URL)
    assert f'href="{LIST_URL}/{locked}"' in listing.text
    assert f'action="{LIST_URL}/{locked}/delete"' not in listing.text
    assert f'name="edit:{locked}:name"' not in listing.text
    assert "Invisible" not in listing.text
    assert (await client.get(f"{LIST_URL}/{denied}")).status_code == 403
    detail = await client.get(f"{LIST_URL}/{locked}")
    assert detail.status_code == 200
    assert "View product" in detail.text
    assert 'name="name"' not in detail.text
    assert 'name="_action"' not in detail.text


@pytest.mark.parametrize("restriction", ["queryset", "model", "object"])
@pytest.mark.parametrize("change", [False, True])
async def test_related_choices_and_forged_values_obey_target_permissions(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    restriction: str,
    change: bool,
) -> None:
    token = await signed_in(client)
    category = await make_category("Hidden category")
    pk = await make_product("Original", category_id=category)
    if restriction == "queryset":
        monkeypatch.setattr(
            CategoryAdmin,
            "get_queryset",
            lambda self, ctx: ModelAdmin.get_queryset(self, ctx).where(Category.id != category),
        )
    else:
        monkeypatch.setattr(
            CategoryAdmin,
            "has_view_permission",
            lambda self, ctx, obj=None: (
                restriction != "model" and (obj is None or obj.id != category)
            ),
        )
    url = f"{LIST_URL}/{pk}" if change else f"{LIST_URL}/add"
    form = await client.get(url)
    assert "Hidden category" not in form.text
    listing = await client.get(LIST_URL)
    assert "Hidden category" not in listing.text
    response = await client.post(
        url,
        data={
            "csrf_token": token,
            **product_form(name="Forged", category_id=str(category)),
        },
    )
    assert response.status_code == 200
    assert "Select a valid choice." in response.text
    assert (await product(pk)).name == "Original"
    async with session_scope() as db:
        from sqlalchemy import select

        assert await db.scalar(select(Product).where(Product.name == "Forged")) is None


async def test_related_choices_require_view_permission_for_non_superuser(
    client: httpx.AsyncClient,
) -> None:
    await make_category("Private category")
    await make_user("writer")
    await grant_permissions("writer", "shop.product.create", "shop.product.read")
    await login_session(client, "writer")
    response = await client.get(f"{LIST_URL}/add")
    assert response.status_code == 200
    assert "Private category" not in response.text


async def test_relation_outside_first_page_stays_selected(
    client: httpx.AsyncClient,
) -> None:
    await signed_in(client)
    async with session_scope() as db:
        db.add_all(Category(name=f"Category {index}") for index in range(500))
    category = await make_category("Current category")
    pk = await make_product("Original", category_id=category)
    response = await client.get(f"{LIST_URL}/{pk}")
    assert "Current category" in response.text
    assert f'<option value="{category}" selected>Current category</option>' in response.text


async def test_readonly_fields_are_visible_without_becoming_writable(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await signed_in(client)
    pk = await make_product("Readonly", price_cents=314159, meta={"note": "JSON value"})
    monkeypatch.setattr(
        ProductAdmin,
        "get_form_fields",
        lambda self, ctx: [
            replace(spec, readonly=True, editable=False) if spec.name == "price_cents" else spec
            for spec in self.get_form_specs()
        ],
    )
    response = await client.get(f"{LIST_URL}/{pk}")
    assert "314,159" in response.text
    assert "JSON value" in response.text
    assert 'name="price_cents"' not in response.text
    assert (
        await client.post(
            f"{LIST_URL}/{pk}",
            data={
                "csrf_token": token,
                **product_form(price_cents="999"),
            },
        )
    ).status_code == 303
    assert (await product(pk)).price_cents == 314159
