from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import Request
from obox_builder.contrib.admin import AdminContext, AdminPaginator, AdminRoute, ModelAdmin, site
from obox_builder.db.session import session_scope
from obox_builder.fields import FieldSpec
from obox_builder.fields.types.text import Text

from tests.admin.conftest import (
    build_app,
    csrf_for,
    grant_permissions,
    login_session,
    make_user,
)
from tests.admin.test_model_views import LIST_URL, get_product, make_product, product_form
from tests.support.apps.shop.admin import ProductAdmin
from tests.support.apps.shop.models import Category, Product

pytestmark = pytest.mark.usefixtures("admin_env")


async def test_dynamic_changelist_hooks_follow_each_request(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_id = await make_product("Needle live", status="live")
    await make_product("Needle draft", status="draft")
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")

    def get_list_display(self: Any, ctx: AdminContext) -> tuple[str, ...]:
        return ("name", "status") if ctx.request.query_params.get("mode") == "compact" else ()

    def get_list_display_links(self: Any, ctx: AdminContext) -> tuple[str, ...]:
        return ("status",) if ctx.request.query_params.get("mode") == "compact" else ("name",)

    def get_list_filter(self: Any, ctx: AdminContext) -> tuple[str, ...]:
        return ("status",) if ctx.request.query_params.get("mode") == "compact" else ()

    def get_search_fields(self: Any, ctx: AdminContext) -> tuple[str, ...]:
        return ("name",) if ctx.request.query_params.get("mode") == "compact" else ()

    def get_list_editable(self: Any, ctx: AdminContext) -> tuple[str, ...]:
        return ("name",) if ctx.request.query_params.get("inline") == "1" else ()

    monkeypatch.setattr(ProductAdmin, "get_list_display", get_list_display)
    monkeypatch.setattr(ProductAdmin, "get_list_display_links", get_list_display_links)
    monkeypatch.setattr(ProductAdmin, "get_list_filter", get_list_filter)
    monkeypatch.setattr(ProductAdmin, "get_search_fields", get_search_fields)
    monkeypatch.setattr(ProductAdmin, "get_list_editable", get_list_editable)

    compact = await client.get(f"{LIST_URL}?mode=compact&q=Needle&f=status:eq:live")
    assert compact.status_code == 200
    assert "Needle live" in compact.text
    assert "Needle draft" not in compact.text
    links = re.findall(rf'<a href="{LIST_URL}/{live_id}">(.*?)</a>', compact.text)
    assert any("live" in link.casefold() for link in links)
    assert "Name" in compact.text
    assert "Status" in compact.text

    plain = await client.get(f"{LIST_URL}?mode=plain&q=Needle")
    assert "Needle live" in plain.text
    assert "Needle draft" in plain.text

    inline = await client.get(f"{LIST_URL}?mode=compact&inline=1")
    assert f'name="edit:{live_id}:name"' in inline.text
    inline_saved = await client.post(
        f"{LIST_URL}/inline?mode=compact&inline=1",
        data={"csrf_token": token, f"edit:{live_id}:name": "Updated live"},
    )
    assert inline_saved.status_code == 303
    updated = await get_product(live_id)
    assert updated is not None
    assert updated.name == "Updated live"


async def test_dynamic_form_fields_fieldsets_and_readonly_fields(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")
    product_id = await make_product("Locked", status="draft", price_cents=150)

    def get_fields(self: Any, ctx: AdminContext) -> tuple[str, ...]:
        return ("name", "status", "price_cents")

    def get_fieldsets(self: Any, ctx: AdminContext) -> tuple[Any, ...]:
        return (
            (
                "Request fields",
                {
                    "fields": (("name", "status"), "price_cents"),
                    "description": "Dynamic layout",
                },
            ),
        )

    def get_readonly_fields(self: Any, ctx: AdminContext) -> tuple[str, ...]:
        return ("name",) if ctx.obj is not None and ctx.obj.name == "Locked" else ()

    def get_exclude(self: Any, ctx: AdminContext) -> tuple[str, ...]:
        return ("price_cents",) if ctx.obj is None else ()

    monkeypatch.setattr(ProductAdmin, "get_fields", get_fields)
    monkeypatch.setattr(ProductAdmin, "get_fieldsets", get_fieldsets)
    monkeypatch.setattr(ProductAdmin, "get_readonly_fields", get_readonly_fields)
    monkeypatch.setattr(ProductAdmin, "get_exclude", get_exclude)

    add_page = await client.get(f"{LIST_URL}/add")
    assert 'name="price_cents"' not in add_page.text
    page = await client.get(f"{LIST_URL}/{product_id}")
    assert page.status_code == 200
    assert "Request fields" in page.text
    assert "Dynamic layout" in page.text
    assert 'name="name"' not in page.text
    assert 'name="status"' in page.text
    assert 'name="category_id"' not in page.text

    response = await client.post(
        f"{LIST_URL}/{product_id}",
        data={
            "csrf_token": token,
            "name": "Forged change",
            "status": "live",
            "price_cents": "250",
        },
    )
    assert response.status_code == 303
    product = await get_product(product_id)
    assert product is not None
    assert (product.name, product.status, product.price_cents) == ("Locked", "live", 250)


async def test_dynamic_relation_fields_and_prepopulation(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    category_id = await _make_category("Books")
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    product_admin = site.get_model_admin_for(Product)
    category_admin = site.get_model_admin_for(Category)
    assert product_admin is not None
    assert category_admin is not None

    monkeypatch.setattr(ProductAdmin, "get_autocomplete_fields", lambda self, ctx: ("category_id",))
    monkeypatch.setattr(
        ProductAdmin,
        "get_prepopulated_fields",
        lambda self, ctx: {"name": ("status",)},
    )
    monkeypatch.setattr(type(category_admin), "search_fields", ("name",))

    page = await client.get(f"{LIST_URL}/add")
    assert 'data-prepopulated-fields=\'{"name": ["status"]}\'' in page.text
    assert (
        'data-relation-picker data-url="/admin/shop/product/autocomplete/category_id"' in page.text
    )

    choices = await client.get(f"{LIST_URL}/autocomplete/category_id?q=Book")
    assert choices.status_code == 200
    assert choices.json()["results"] == [{"id": str(category_id), "text": "Books"}]


async def test_extra_form_fields_validate_save_and_use_custom_widget(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")
    admin = site.get_model_admin_for(Product)
    assert admin is not None
    monkeypatch.setattr(
        ProductAdmin,
        "fields_config",
        {"name": {"widget_template": "shop/admin/widgets/text_input.html"}},
    )
    monkeypatch.setattr(admin, "_specs", None)

    def extra_fields(self: Any, ctx: AdminContext) -> tuple[FieldSpec, ...]:
        return (
            FieldSpec(
                name="review_note",
                label="Review note",
                type=Text(),
                required=True,
                nullable=False,
                options={"widget_template": "shop/admin/widgets/text_input.html"},
            ),
        )

    saved_notes: list[tuple[bool, str]] = []

    async def save_model(
        self: Any, ctx: AdminContext, obj: Product, form: Any, *, change: bool
    ) -> None:
        saved_notes.append((change, form.cleaned["review_note"]))
        await ModelAdmin.save_model(self, ctx, obj, form, change=change)

    monkeypatch.setattr(ProductAdmin, "get_extra_form_fields", extra_fields)
    monkeypatch.setattr(ProductAdmin, "save_model", save_model)

    form_page = await client.get(f"{LIST_URL}/add")
    assert 'data-custom-widget="name"' in form_page.text
    assert 'data-custom-widget="review_note"' in form_page.text

    invalid = await client.post(
        f"{LIST_URL}/add",
        data={
            "csrf_token": token,
            **product_form(price_cents="not-a-number"),
            "review_note": "<b>keep this escaped</b>",
        },
    )
    assert invalid.status_code == 200
    assert "keep this escaped" in invalid.text
    assert "&lt;b&gt;keep this escaped&lt;/b&gt;" in invalid.text
    assert not saved_notes

    created = await client.post(
        f"{LIST_URL}/add",
        data={
            "csrf_token": token,
            **product_form(),
            "review_note": "Approved by reviewer",
        },
    )
    assert created.status_code == 303
    assert saved_notes == [(False, "Approved by reviewer")]
    async with session_scope() as db:
        product = await db.scalar(sa.select(Product).where(Product.name == "Widget"))
        assert product is not None
        product_id = product.id
        assert not hasattr(product, "review_note")

    changed = await client.post(
        f"{LIST_URL}/{product_id}",
        data={
            "csrf_token": token,
            **product_form(name="Widget update"),
            "review_note": "Follow-up review",
        },
    )
    assert changed.status_code == 303
    assert saved_notes == [
        (False, "Approved by reviewer"),
        (True, "Follow-up review"),
    ]


async def test_custom_model_routes_enforce_login_permissions_and_csrf(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await make_user("viewer")
    await grant_permissions("viewer", "shop.product.read")
    await login_session(client, "viewer")
    token = await csrf_for("viewer")

    async def report(ctx: AdminContext, pk: int, request: Request) -> dict[str, Any]:
        product = await ctx.db.get(Product, pk)
        assert product is not None
        return {"username": ctx.user.username, "name": product.name, "path": request.url.path}

    async def rebuild(ctx: AdminContext) -> dict[str, str]:
        return {"username": ctx.user.username}

    async def restricted(ctx: AdminContext) -> dict[str, str]:
        return {"username": ctx.user.username}

    async def custom_lookup(ctx: AdminContext) -> dict[str, str]:
        return {"username": ctx.user.username, "route": "custom"}

    routes = (
        AdminRoute("reports/{pk}", report, "report"),
        AdminRoute("autocomplete/category_id", custom_lookup, "custom_lookup"),
        AdminRoute("restricted", restricted, "restricted", permission="change"),
        AdminRoute("rebuild", rebuild, "rebuild", methods=("POST",), permission="change"),
    )
    monkeypatch.setattr(ProductAdmin, "get_urls", lambda self: routes)
    product_id = await make_product("Route target")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app()),
        base_url="http://testserver",
        cookies=client.cookies,
    ) as routed:
        routed.cookies.clear()
        assert (await routed.get(f"{LIST_URL}/reports/{product_id}")).status_code == 401
        routed.cookies.update(client.cookies)
        assert (await routed.get(f"{LIST_URL}/reports/{product_id}")).status_code == 200
        assert (await routed.get(f"{LIST_URL}/reports/{product_id}")).json() == {
            "username": "viewer",
            "name": "Route target",
            "path": f"{LIST_URL}/reports/{product_id}",
        }
        assert (await routed.get(f"{LIST_URL}/restricted")).status_code == 403
        custom = await routed.get(f"{LIST_URL}/autocomplete/category_id")
        assert custom.status_code == 200
        assert custom.json() == {"username": "viewer", "route": "custom"}
        assert (
            await routed.post(f"{LIST_URL}/rebuild", headers={"x-csrf-token": token})
        ).status_code == 403

        await make_user("editor", is_superuser=True)
        await login_session(client, "editor")
        editor_token = await csrf_for("editor")
        routed.cookies.update(client.cookies)
        missing_csrf = await routed.post(f"{LIST_URL}/rebuild")
        assert missing_csrf.status_code == 403
        accepted = await routed.post(f"{LIST_URL}/rebuild", headers={"x-csrf-token": editor_token})
        assert accepted.status_code == 200
        assert accepted.json() == {"username": "editor"}
        form_token = await routed.post(f"{LIST_URL}/rebuild", data={"csrf_token": editor_token})
        assert form_token.status_code == 200
        duplicate_tokens = await routed.post(
            f"{LIST_URL}/rebuild", data={"csrf_token": [editor_token, editor_token]}
        )
        assert duplicate_tokens.status_code == 403


async def test_custom_paginator_and_countless_next_previous_pages(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await make_product("Item 1")
    await make_product("Item 2")
    await make_product("Item 3")
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")

    class CountlessPaginator(AdminPaginator):
        async def count(self) -> int:
            raise AssertionError("count must not run when show_full_result_count is false")

    monkeypatch.setattr(ProductAdmin, "show_full_result_count", False)
    monkeypatch.setattr(
        ProductAdmin,
        "get_paginator",
        lambda self, ctx, queryset: CountlessPaginator(ctx, queryset, show_full_result_count=False),
    )

    first = await client.get(f"{LIST_URL}?size=2&sort=name")
    assert first.status_code == 200
    assert "Item 1" in first.text
    assert "Item 2" in first.text
    assert "Item 3" not in first.text
    assert "page 1" in first.text
    assert ">Next</a>" in first.text
    assert '<ul class="pagination">' in first.text

    second = await client.get(f"{LIST_URL}?size=2&sort=name&page=2")
    assert "Item 3" in second.text
    assert "Item 1" not in second.text
    assert ">Previous</a>" in second.text
    assert ">Next</a>" not in second.text

    class CustomCountPaginator(AdminPaginator):
        async def count(self) -> int:
            return 123

    monkeypatch.setattr(
        ProductAdmin,
        "get_paginator",
        lambda self, ctx, queryset: CustomCountPaginator(ctx, queryset),
    )
    custom = await client.get(f"{LIST_URL}?size=2&sort=name")
    assert "123 records" in custom.text

    monkeypatch.setattr(
        ProductAdmin,
        "get_paginator",
        lambda self, ctx, queryset: AdminPaginator(ctx, queryset),
    )
    monkeypatch.setattr(
        ProductAdmin,
        "get_list_per_page",
        lambda self, ctx: 1 if ctx.request.query_params.get("page_mode") == "one" else 50,
    )
    one = await client.get(f"{LIST_URL}?page_mode=one&sort=name")
    assert "Item 1" in one.text
    assert "Item 2" not in one.text
    two = await client.get(f"{LIST_URL}?page_mode=one&sort=name&page=2")
    assert "Item 2" in two.text
    assert "Item 1" not in two.text


async def _make_category(name: str) -> int:
    async with session_scope() as db:
        category = Category(name=name)
        db.add(category)
        await db.flush()
        return category.id
