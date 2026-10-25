from __future__ import annotations

from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.contrib.admin import SimpleListFilter, site
from obox_builder.db.session import session_scope

from tests.admin.conftest import (
    build_app,
    csrf_for,
    deletion_confirmation,
    grant_permissions,
    login_session,
    make_user,
)
from tests.support.apps.shop.models import Category, Order, Product

pytestmark = pytest.mark.usefixtures("admin_env")

LIST_URL = "/admin/shop/product"
ADD_URL = "/admin/shop/product/add"


async def make_category(name: str) -> int:
    async with session_scope() as db:
        category = Category(name=name)
        db.add(category)
        await db.flush()
        return category.id


async def make_product(name: str, **values: Any) -> int:
    async with session_scope() as db:
        product = Product(name=name, **values)
        db.add(product)
        await db.flush()
        return product.id


async def get_product(product_id: int) -> Product | None:
    async with session_scope() as db:
        return await db.get(Product, product_id)


def product_form(**overrides: str) -> dict[str, str]:
    data = {
        "name": "Widget",
        "status": "draft",
        "price_cents": "100",
        "is_active": "true",
        "category_id": "",
    }
    data.update(overrides)
    return data


# -- access control ---------------------------------------------------------


async def test_list_redirects_anonymous_users_to_login(client: httpx.AsyncClient) -> None:
    response = await client.get(LIST_URL, headers={"accept": "text/html"})
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


async def test_view_only_user_gets_denied_on_mutations(client: httpx.AsyncClient) -> None:
    product_id = await make_product("Widget")
    await make_user("viewer")
    await grant_permissions("viewer", "shop.view_product")
    await login_session(client, "viewer")
    listing = await client.get(LIST_URL, headers={"accept": "text/html"})
    assert listing.status_code == 200
    assert "Widget" in listing.text
    assert "New product" not in listing.text
    assert (await client.get(ADD_URL, headers={"accept": "text/html"})).status_code == 403
    detail = await client.get(f"{LIST_URL}/{product_id}", headers={"accept": "text/html"})
    assert detail.status_code == 200
    assert "View product" in detail.text
    assert "Widget" in detail.text
    assert 'name="_action"' not in detail.text
    token = await csrf_for("viewer")
    assert (
        await client.post(
            f"{LIST_URL}/{product_id}", data={"csrf_token": token, **product_form(name="Forged")}
        )
    ).status_code == 403
    assert (
        await client.get(f"{LIST_URL}/{product_id}/delete", headers={"accept": "text/html"})
    ).status_code == 403
    product = await get_product(product_id)
    assert product is not None
    assert product.name == "Widget"


async def test_user_with_crud_permissions_can_manage_products(client: httpx.AsyncClient) -> None:
    await make_user("editor")
    await grant_permissions(
        "editor",
        "shop.view_product",
        "shop.add_product",
        "shop.change_product",
        "shop.delete_product",
    )
    await login_session(client, "editor")
    token = await csrf_for("editor")
    created = await client.post(
        ADD_URL, data={"csrf_token": token, **product_form()}, follow_redirects=False
    )
    assert created.status_code == 303
    async with session_scope() as db:
        product = await db.scalar(sa.select(Product).where(Product.name == "Widget"))
    assert product is not None
    assert product.status == "draft"


# -- create / update / delete ----------------------------------------------


async def test_create_product_from_the_form(client: httpx.AsyncClient) -> None:
    category_id = await make_category("Books")
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")
    response = await client.post(
        ADD_URL,
        data={
            "csrf_token": token,
            **product_form(
                name="Novel", status="live", price_cents="250", category_id=str(category_id)
            ),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == LIST_URL
    async with session_scope() as db:
        product = await db.scalar(sa.select(Product).where(Product.name == "Novel"))
    assert product is not None
    assert (product.status, product.price_cents) == ("live", 250)
    assert product.category_id == category_id
    assert product.is_active is True
    assert product.meta == {}

    listing = await client.get(LIST_URL, headers={"accept": "text/html"})
    assert "Novel" in listing.text
    assert "was created" in listing.text
    # Flash messages are consumed by the first page that renders them.
    again = await client.get(LIST_URL, headers={"accept": "text/html"})
    assert "was created" not in again.text


async def test_create_accepts_csrf_from_the_header(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")
    response = await client.post(
        ADD_URL,
        data=product_form(),
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303


async def test_csrf_is_enforced(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    assert (await client.post(ADD_URL, data=product_form())).status_code == 403
    bad = await client.post(ADD_URL, data={"csrf_token": "wrong", **product_form()})
    assert bad.status_code == 403


async def test_validation_errors_are_shown_per_field(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")
    response = await client.post(
        ADD_URL,
        data={"csrf_token": token, **product_form(name="", status="imaginary", price_cents="abc")},
    )
    assert response.status_code == 200
    assert "This field is required." in response.text
    assert "Select a valid choice." in response.text
    assert "Input should be a valid integer" in response.text
    assert 'value="abc"' in response.text


async def test_invalid_json_is_rejected_before_save(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    token = await login_session(client, "alice")
    csrf = await csrf_for("alice", token)
    response = await client.post(ADD_URL, data={"csrf_token": csrf, **product_form(meta="{bad")})
    assert response.status_code == 200
    assert "Enter valid JSON." in response.text
    async with session_scope() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(Product)) == 0


async def test_json_is_editable_and_primary_key_injection_is_ignored(
    client: httpx.AsyncClient,
) -> None:
    product_id = await make_product("Widget")
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")
    response = await client.post(
        f"{LIST_URL}/{product_id}",
        data={
            "csrf_token": token,
            **product_form(name="Renamed"),
            "meta": '{"injected": true}',
            "id": "999",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    product = await get_product(product_id)
    assert product is not None
    assert product.name == "Renamed"
    assert product.meta == {"injected": True}
    assert product.id == product_id


async def test_change_form_prefills_values_and_link_choices(client: httpx.AsyncClient) -> None:
    category_id = await make_category("Books")
    product_id = await make_product("Novel", category_id=category_id, price_cents=250)
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    response = await client.get(f"{LIST_URL}/{product_id}", headers={"accept": "text/html"})
    assert response.status_code == 200
    assert 'value="Novel"' in response.text
    assert 'value="250"' in response.text
    assert f'<option value="{category_id}" selected>Books</option>' in response.text
    assert 'name="meta"' in response.text
    assert 'name="id"' not in response.text
    assert 'name="created_at"' not in response.text


async def test_save_as_new_creates_a_distinct_record(client: httpx.AsyncClient) -> None:
    product_id = await make_product("Original")
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")
    response = await client.post(
        f"{LIST_URL}/{product_id}",
        data={"csrf_token": token, **product_form(name="Copy"), "_action": "save_as_new"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    async with session_scope() as db:
        names = set((await db.scalars(sa.select(Product.name))).all())
    assert names == {"Original", "Copy"}


async def test_custom_simple_list_filter_applies_and_rejects_unknown_values(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.support.apps.shop.models import Product

    class ActiveFilter(SimpleListFilter):
        title = "Active state"
        parameter_name = "active_state"

        def lookups(self) -> tuple[tuple[str, str], ...]:
            return (("active", "Active"), ("inactive", "Inactive"))

        def queryset(self, queryset: sa.Select[Any], value: str) -> sa.Select[Any]:
            return sa.select(Product)

    admin = site.get_model_admin_for(Product)
    assert admin is not None
    monkeypatch.setattr(type(admin), "list_filter", (ActiveFilter,))
    default_queryset = admin.get_queryset
    monkeypatch.setattr(
        admin,
        "get_queryset",
        lambda ctx: default_queryset(ctx).where(Product.name == "Active result"),
    )
    await make_product("Active result", is_active=True)
    await make_product("Inactive result", is_active=False)
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")

    response = await client.get(f"{LIST_URL}?active_state=active")
    assert response.status_code == 200
    assert "Active result" in response.text
    assert "Inactive result" not in response.text
    invalid = await client.get(f"{LIST_URL}?active_state=unknown")
    assert invalid.status_code == 400


async def test_relation_search_and_filter_follow_related_visibility(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    category_id = await make_category("Books")
    other_category_id = await make_category("Games")
    await make_product("Novel", category_id=category_id)
    await make_product("Controller", category_id=other_category_id)
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    admin = site.get_model_admin_for(Product)
    assert admin is not None
    monkeypatch.setattr(type(admin), "search_fields", ("category__name",))
    monkeypatch.setattr(type(admin), "list_filter", ("category__name",))

    searched = await client.get(f"{LIST_URL}?q=Books")
    filtered = await client.get(f"{LIST_URL}?f=category__name:eq:Books")
    assert searched.status_code == filtered.status_code == 200
    assert "Novel" in searched.text
    assert "Controller" not in searched.text
    assert "Novel" in filtered.text
    assert "Controller" not in filtered.text


async def test_related_list_column_honors_related_scope_and_configured_link(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    category_id = await make_category("Private category")
    product_id = await make_product("Novel", category_id=category_id)
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    product_admin = site.get_model_admin_for(Product)
    category_admin = site.get_model_admin_for(Category)
    assert product_admin is not None
    assert category_admin is not None
    monkeypatch.setattr(type(product_admin), "list_display", ("id", "category__name", "name"))
    monkeypatch.setattr(type(product_admin), "list_display_links", ("category__name",))
    original_queryset = type(category_admin).get_queryset
    monkeypatch.setattr(
        type(category_admin),
        "get_queryset",
        lambda self, ctx: original_queryset(self, ctx).where(Category.name != "Private category"),
    )

    response = await client.get(LIST_URL)

    assert response.status_code == 200
    assert "Novel" in response.text
    assert "Private category" not in response.text
    assert (
        f'<a href="{LIST_URL}/{product_id}"><span class="text-secondary">&mdash;</span></a>'
        in response.text
    )
    assert f'href="{LIST_URL}/{product_id}">Novel</a>' not in response.text


async def test_selected_list_display_link_overrides_a_relation_cell_link(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    category_id = await make_category("Books")
    product_id = await make_product("Novel", category_id=category_id)
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    admin = site.get_model_admin_for(Product)
    assert admin is not None
    monkeypatch.setattr(type(admin), "list_display", ("category_id", "name"))
    monkeypatch.setattr(type(admin), "list_display_links", ("category_id",))

    response = await client.get(LIST_URL)

    assert response.status_code == 200
    assert f'<a href="{LIST_URL}/{product_id}">Books</a>' in response.text
    assert f'href="/admin/shop/category/{category_id}">Books</a>' not in response.text


async def test_fields_config_widget_settings_render_on_the_form(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    admin = site.get_model_admin_for(Product)
    assert admin is not None
    monkeypatch.setattr(
        type(admin),
        "fields_config",
        {"name": {"widget": "textarea", "rows": 8, "placeholder": "Public name"}},
    )
    admin._specs = None
    admin._schema = None

    response = await client.get(ADD_URL)

    assert response.status_code == 200
    assert 'rows="8"' in response.text
    assert 'placeholder="Public name"' in response.text


async def test_delete_requires_confirmation_then_removes(client: httpx.AsyncClient) -> None:
    product_id = await make_product("Widget")
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")
    confirm = await client.get(f"{LIST_URL}/{product_id}/delete", headers={"accept": "text/html"})
    assert confirm.status_code == 200
    assert "Are you sure" in confirm.text
    deleted = await client.post(
        f"{LIST_URL}/{product_id}/delete",
        data={"csrf_token": token, "_delete_confirmation": deletion_confirmation(confirm)},
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert await get_product(product_id) is None
    listing = await client.get(LIST_URL, headers={"accept": "text/html"})
    assert "was deleted" in listing.text


async def test_delete_is_blocked_by_foreign_key(client: httpx.AsyncClient) -> None:
    category_id = await make_category("Books")
    await make_product("Novel", category_id=category_id)
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")
    response = await client.post(
        f"/admin/shop/category/{category_id}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert "prevents deletion" in response.text

    async with session_scope() as db:
        category = await db.get(Category, category_id)
    assert category is not None


async def test_duplicate_unique_value_is_reported(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")
    first = await client.post(
        "/admin/shop/order/add",
        data={"csrf_token": token, "reference": "ORD-1"},
        follow_redirects=False,
    )
    assert first.status_code == 303
    duplicate = await client.post(
        "/admin/shop/order/add", data={"csrf_token": token, "reference": "ORD-1"}
    )
    assert duplicate.status_code == 200
    assert "Could not save" in duplicate.text
    async with session_scope() as db:
        orders = (await db.scalars(sa.select(Order))).all()
    assert len(orders) == 1


# -- list behaviour ---------------------------------------------------------


async def test_search_filters_rows(client: httpx.AsyncClient) -> None:
    await make_product("Widget")
    await make_product("Gadget")
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    response = await client.get(f"{LIST_URL}?q=wid", headers={"accept": "text/html"})
    assert response.status_code == 200
    assert "Widget" in response.text
    assert "Gadget" not in response.text


async def test_sorting_toggles_direction(client: httpx.AsyncClient) -> None:
    await make_product("Alpha")
    await make_product("Zeta")
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    ascending = await client.get(f"{LIST_URL}?sort=name", headers={"accept": "text/html"})
    assert ascending.text.index("Alpha") < ascending.text.index("Zeta")
    descending = await client.get(f"{LIST_URL}?sort=-name", headers={"accept": "text/html"})
    assert descending.text.index("Zeta") < descending.text.index("Alpha")


async def test_boolean_and_choice_filters(client: httpx.AsyncClient) -> None:
    await make_product("Running", status="live")
    await make_product("Planned", status="draft")
    await make_product("Archived", status="draft", is_active=False)
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    live = await client.get(f"{LIST_URL}?f=status:eq:live", headers={"accept": "text/html"})
    assert "Running" in live.text
    assert "Planned" not in live.text
    inactive = await client.get(f"{LIST_URL}?f=is_active:eq:false", headers={"accept": "text/html"})
    assert "Archived" in inactive.text
    assert "Planned" not in inactive.text


async def test_pagination(client: httpx.AsyncClient) -> None:
    for index in range(3):
        await make_product(f"Item {index}")
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    first = await client.get(f"{LIST_URL}?size=2&sort=name", headers={"accept": "text/html"})
    assert "Item 0" in first.text
    assert "Item 1" in first.text
    assert "Item 2" not in first.text
    second = await client.get(
        f"{LIST_URL}?size=2&sort=name&page=2", headers={"accept": "text/html"}
    )
    assert "Item 2" in second.text
    assert "Item 0" not in second.text


async def test_link_cells_link_to_the_target_admin(client: httpx.AsyncClient) -> None:
    category_id = await make_category("Books")
    await make_product("Novel", category_id=category_id)
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    response = await client.get(LIST_URL, headers={"accept": "text/html"})
    assert response.status_code == 200
    assert f'href="/admin/shop/category/{category_id}"' in response.text


async def test_sidebar_lists_registered_models(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    response = await client.get(LIST_URL, headers={"accept": "text/html"})
    assert 'href="/admin/shop/product"' in response.text
    assert 'href="/admin/shop/category"' in response.text
    assert 'href="/admin/shop/order"' in response.text


# -- error handling ---------------------------------------------------------


async def test_invalid_query_parameters_get_a_400(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    bad_filter = await client.get(f"{LIST_URL}?f=nope:eq:1", headers={"accept": "text/html"})
    assert bad_filter.status_code == 400
    assert "Bad request" in bad_filter.text
    bad_sort = await client.get(f"{LIST_URL}?sort=nope", headers={"accept": "text/html"})
    assert bad_sort.status_code == 400


async def test_unknown_objects_produce_404(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    for url in (
        f"{LIST_URL}/99999",
        f"{LIST_URL}/99999/delete",
        f"{LIST_URL}/not-a-number",
    ):
        response = await client.get(url, headers={"accept": "text/html"})
        assert response.status_code == 404, url
        assert "Page not found" in response.text


async def test_base_path_admin_serves_crud_at_the_new_url(client: httpx.AsyncClient) -> None:
    from obox_builder.contrib.admin.site import site

    from tests.support.apps.shop.admin import ProductAdmin

    class TopicsAdmin(ProductAdmin):
        base_path = "topics"

    await make_product("Widget")
    await make_user("alice", is_superuser=True)
    site.register(Product, TopicsAdmin)
    try:
        transport = httpx.ASGITransport(app=build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            await login_session(http, "alice")
            listing = await http.get("/admin/topics", headers={"accept": "text/html"})
            assert listing.status_code == 200
            assert "Widget" in listing.text
            add_form = await http.get("/admin/topics/add", headers={"accept": "text/html"})
            assert add_form.status_code == 200
            old = await http.get(LIST_URL, headers={"accept": "text/html"})
            assert old.status_code == 404
    finally:
        site.register(Product, ProductAdmin)
