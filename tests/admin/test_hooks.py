from __future__ import annotations

from typing import Any, cast

import httpx
import pytest
import sqlalchemy as sa
from fastapi.responses import RedirectResponse
from obox_builder.contrib.admin import ModelAdmin
from obox_builder.db.session import session_scope
from pydantic import create_model, model_validator

from tests.admin.test_model_views import LIST_URL, get_product, make_product, product_form
from tests.admin.test_permissions import signed_in
from tests.support.apps.shop.admin import ProductAdmin
from tests.support.apps.shop.models import Order, Product


async def test_form_fields_use_request_and_object_without_shared_state(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await signed_in(client)
    pk = await make_product("Original", price_cents=123)
    seen: list[Any] = []

    def fields(self: Any, ctx: Any) -> Any:
        seen.append(ctx.obj.id if ctx.obj is not None else None)
        return [
            spec
            for spec in self.get_form_specs()
            if ctx.request.query_params.get("narrow") != "1" or spec.name == "name"
        ]

    monkeypatch.setattr(ProductAdmin, "get_form_fields", fields)
    narrow = await client.get(f"{LIST_URL}/{pk}?narrow=1")
    assert 'name="name"' in narrow.text
    assert 'name="price_cents"' not in narrow.text
    normal = await client.get(f"{LIST_URL}/{pk}")
    assert 'name="price_cents"' in normal.text
    response = await client.post(
        f"{LIST_URL}/{pk}?narrow=1",
        data={
            "csrf_token": token,
            **product_form(name="Changed", price_cents="999"),
        },
    )
    assert response.status_code == 303
    product = await get_product(pk)
    assert product is not None
    assert (product.name, product.price_cents) == ("Changed", 123)
    assert set(seen) == {pk}
    assert (await client.get(f"{LIST_URL}/add?narrow=1")).status_code == 200
    assert seen[-1] is None


async def test_custom_form_validation_stops_save_hooks(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await signed_in(client)
    calls: list[bool] = []

    def get_form(self: Any, ctx: Any, *, change: bool = False) -> Any:
        def positive(form: Any) -> Any:
            if form.price_cents < 0:
                raise ValueError("Price must be positive.")
            return form

        return create_model(
            "ProductForm",
            __base__=ModelAdmin.get_form(self, ctx, change=change),
            __validators__={"positive": cast(Any, model_validator(mode="after")(positive))},
        )

    async def save(self: Any, ctx: Any, obj: Any, form: Any, *, change: bool) -> None:
        calls.append(change)
        await ModelAdmin.save_model(self, ctx, obj, form, change=change)

    monkeypatch.setattr(ProductAdmin, "get_form", get_form)
    monkeypatch.setattr(ProductAdmin, "save_model", save)
    response = await client.post(
        f"{LIST_URL}/add",
        data={
            "csrf_token": token,
            **product_form(price_cents="-1"),
        },
    )
    assert response.status_code == 200
    assert "Price must be positive." in response.text
    assert not calls
    async with session_scope() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(Product)) == 0


@pytest.mark.parametrize("operation", ["create", "update", "inline"])
async def test_save_hooks_run_in_order_with_actor_object_and_validated_form(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    token = await signed_in(client)
    pk = await make_product("Original") if operation != "create" else None
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(ProductAdmin, "list_editable", ("name",))

    async def save(self: Any, ctx: Any, obj: Any, form: Any, *, change: bool) -> None:
        assert ctx.obj is obj
        calls.append(("save", ctx.user.username, change, form.cleaned["name"]))
        obj.name += " by operator"
        await ModelAdmin.save_model(self, ctx, obj, form, change=change)

    async def related(self: Any, ctx: Any, obj: Any, form: Any, *, change: bool) -> None:
        assert obj.id is not None
        calls.append(("related", change))
        ctx.db.add(Order(reference=f"saved-{obj.id}"))

    async def response(self: Any, ctx: Any, obj: Any) -> Any:
        assert await ctx.db.scalar(sa.select(Order).where(Order.reference == f"saved-{obj.id}"))
        calls.append(("response", obj.id))
        return RedirectResponse("/custom-result", status_code=303)

    monkeypatch.setattr(ProductAdmin, "save_model", save)
    monkeypatch.setattr(ProductAdmin, "save_related", related)
    monkeypatch.setattr(ProductAdmin, "response_add", response)
    monkeypatch.setattr(ProductAdmin, "response_change", response)
    suffix = "/add" if pk is None else f"/{pk}"
    data = {"csrf_token": token, **product_form(name="Changed")}
    if operation == "inline":
        suffix, data = "/inline", {"csrf_token": token, f"edit:{pk}:name": "Changed"}
    result = await client.post(LIST_URL + suffix, data=data)
    assert result.status_code == 303
    assert calls[:2] == [
        ("save", "operator", operation != "create", "Changed"),
        ("related", operation != "create"),
    ]
    assert len(calls) == (2 if operation == "inline" else 3)
    assert result.headers["location"] == (LIST_URL if operation == "inline" else "/custom-result")
    async with session_scope() as db:
        assert await db.scalar(sa.select(Product).where(Product.name == "Changed by operator"))
        assert await db.scalar(sa.select(sa.func.count()).select_from(Order)) == 1


@pytest.mark.parametrize("operation", ["create", "update", "inline"])
async def test_related_failure_rolls_back_parent_and_entire_inline_batch(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    token = await signed_in(client)
    first = await make_product("First")
    second = await make_product("Second")
    async with session_scope() as db:
        db.add(Order(reference="duplicate"))
    monkeypatch.setattr(ProductAdmin, "list_editable", ("name",))

    async def related(self: Any, ctx: Any, obj: Any, form: Any, *, change: bool) -> None:
        if obj.name != "Changed first":
            ctx.db.add(Order(reference="duplicate"))

    monkeypatch.setattr(ProductAdmin, "save_related", related)
    suffix = "/add" if operation == "create" else f"/{first}"
    data = {"csrf_token": token, **product_form(name="Changed")}
    if operation == "inline":
        suffix = "/inline"
        data = {
            "csrf_token": token,
            f"edit:{first}:name": "Changed first",
            f"edit:{second}:name": "Changed second",
        }
    response = await client.post(LIST_URL + suffix, data=data)
    assert response.status_code == 200
    assert "database rejected" in response.text
    async with session_scope() as db:
        products = (await db.scalars(sa.select(Product).order_by(Product.id))).all()
        assert [product.name for product in products] == ["First", "Second"]
        assert await db.scalar(sa.select(sa.func.count()).select_from(Order)) == 1


@pytest.mark.parametrize("bulk", [False, True])
async def test_delete_hooks_cover_single_and_bulk_deletion(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    bulk: bool,
) -> None:
    token = await signed_in(client)
    pk = await make_product("Delete me")
    calls: list[str] = []

    async def delete(self: Any, ctx: Any, obj: Any) -> None:
        assert ctx.obj is obj
        calls.append("delete")
        ctx.db.add(Order(reference=f"deleted-{obj.id}"))
        await ModelAdmin.delete_model(self, ctx, obj)

    async def delete_many(self: Any, ctx: Any, queryset: Any) -> None:
        calls.append("queryset")
        await ModelAdmin.delete_queryset(self, ctx, queryset)

    async def response(self: Any, ctx: Any, object_id: Any) -> Any:
        assert object_id == pk
        calls.append("response")
        return RedirectResponse("/deleted", status_code=303)

    monkeypatch.setattr(ProductAdmin, "delete_model", delete)
    monkeypatch.setattr(ProductAdmin, "delete_queryset", delete_many)
    monkeypatch.setattr(ProductAdmin, "response_delete", response)
    suffix = "/actions" if bulk else f"/{pk}/delete"
    result = await client.post(
        LIST_URL + suffix,
        data={
            "csrf_token": token,
            "action": "delete_selected",
            "_selected_action": str(pk),
        },
    )
    assert result.status_code == 303
    assert calls == (["queryset", "delete"] if bulk else ["delete", "response"])
    assert await get_product(pk) is None
    async with session_scope() as db:
        assert await db.scalar(sa.select(Order).where(Order.reference == f"deleted-{pk}"))
