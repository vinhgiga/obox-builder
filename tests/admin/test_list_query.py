from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.contrib.admin.models import SavedView
from obox_builder.db.session import session_scope

from tests.admin.conftest import csrf_for, grant_permissions, login_session, make_user
from tests.admin.test_model_views import make_category, make_product
from tests.support.apps.shop.admin import ProductAdmin
from tests.support.apps.shop.models import Order, Product

pytestmark = pytest.mark.usefixtures("admin_env")
URL = "/admin/shop/product"


@pytest.fixture(autouse=True)
async def close_query_engine(client: httpx.AsyncClient) -> AsyncIterator[None]:
    from obox_builder.db import dispose

    try:
        yield
    finally:
        await dispose()


async def signed_in(client: httpx.AsyncClient, name: str = "alice") -> str:
    await make_user(name, is_superuser=True)
    await login_session(client, name)
    return await csrf_for(name)


def tree(conjunction: str, *children: dict[str, Any]) -> dict[str, Any]:
    return {"conjunction": conjunction, "children": list(children)}


def term(name: str, op: str, value: Any) -> dict[str, Any]:
    return {"field": name, "op": op, "value": value}


async def test_nested_filters_combine_with_legacy_filter_and_search(
    client: httpx.AsyncClient,
) -> None:
    await signed_in(client)
    await make_product("Good cheap", price_cents=5, status="live")
    await make_product("Good costly", price_cents=50, status="draft")
    await make_product("Bad cheap", price_cents=5, status="draft")
    await make_product("Good inactive", price_cents=5, is_active=False)
    config = tree(
        "or",
        term("status", "eq", "live"),
        tree("and", term("price_cents", "gte", 40), term("price_cents", "lte", 60)),
    )
    response = await client.get(
        URL, params={"filter": json.dumps(config), "q": "Good", "f": "is_active:eq:true"}
    )
    assert response.status_code == 200
    assert "Good cheap" in response.text
    assert "Good costly" in response.text
    assert "Bad cheap" not in response.text
    assert "Good inactive" not in response.text


async def test_groups_count_all_filtered_rows_across_pages(client: httpx.AsyncClient) -> None:
    await signed_in(client)
    for index in range(4):
        await make_product(f"Live {index}", status="live")
    await make_product("Draft", status="draft")
    response = await client.get(
        URL, params={"f": "status:eq:live", "group": "status,is_active", "size": 2}
    )
    assert response.status_code == 200
    assert 'data-group-header="1"' in response.text
    assert 'data-group-header="2"' in response.text
    assert "Status: Live" in response.text
    group_count = r"data-group-toggle[^>]*>[^<]*<span[^>]*>4</span>"
    assert len(re.findall(group_count, response.text)) == 2
    header = response.text.split("<thead>")[1].split("</thead>")[0]
    assert f'colspan="{header.count("<th ")}"' in response.text
    assert response.text.count("<tr data-group-row") == 2
    assert "Draft" not in response.text.split("<tbody>")[1]
    second = await client.get(
        URL, params={"f": "status:eq:live", "group": "status,is_active", "size": 2, "page": 2}
    )
    assert second.status_code == 200
    assert len(re.findall(group_count, second.text)) == 2


async def test_group_null_link_and_primary_key(client: httpx.AsyncClient) -> None:
    await signed_in(client)
    category = await make_category("Books")
    await make_product("Linked", category_id=category)
    await make_product("Unlinked")
    response = await client.get(URL, params={"group": "category_id,id"})
    assert response.status_code == 200
    assert "Category: Books" in response.text
    assert "Category: Empty" in response.text


async def test_groups_respect_queryset_and_search(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await signed_in(client)
    await make_product("Visible", status="live", is_active=True)
    await make_product("Visible restricted", status="live", is_active=False)
    await make_product("Unmatched", status="live", is_active=True)
    original = ProductAdmin.get_queryset
    monkeypatch.setattr(
        ProductAdmin, "get_queryset", lambda self, ctx: original(self, ctx).where(Product.is_active)
    )
    response = await client.get(URL, params={"q": "Visible", "group": "status"})
    assert response.status_code == 200
    assert "Visible restricted" not in response.text
    assert "Unmatched" not in response.text
    assert ">1</span>" in response.text


@pytest.mark.parametrize(
    "params",
    [
        {"f": "created_at:gte:bad"},
        {"f": "price_cents:gt:bad"},
        {"f": "status:eq:bad"},
        {"group": "meta"},
    ],
)
async def test_invalid_values_are_bad_requests(client: httpx.AsyncClient, params: dict) -> None:
    await signed_in(client)
    assert (await client.get(URL, params=params)).status_code == 400


async def test_date_filter_uses_typed_value(client: httpx.AsyncClient) -> None:
    await signed_in(client)
    await make_product("Today")
    response = await client.get(URL, params={"f": "created_at:gte:2000-01-01T00:00:00Z"})
    assert response.status_code == 200
    assert "Today" in response.text


async def test_saved_view_round_trip_override_update_and_delete(client: httpx.AsyncClient) -> None:
    token = await signed_in(client)
    await make_product("Published", status="live")
    await make_product("Planned", status="draft")
    config = tree("and", term("status", "eq", "live"))
    response = await client.post(
        f"{URL}/views",
        params={"filter": json.dumps(config), "sort": "name", "group": "status", "page": 7},
        data={"csrf_token": token, "name": "Published only"},
    )
    assert response.status_code == 303
    location = response.headers["location"]
    listing = await client.get(location)
    assert listing.status_code == 200
    assert "Published" in listing.text
    assert "Planned" not in listing.text
    assert "Status: Live" in listing.text
    async with session_scope() as db:
        saved = await db.scalar(sa.select(SavedView))
        assert saved is not None
        assert "page" not in saved.config
        pk = saved.id
    override = await client.get(
        location,
        params={"view": pk, "filter": json.dumps(tree("and", term("status", "eq", "draft")))},
    )
    assert override.status_code == 200
    assert "Planned" in override.text
    update = await client.post(
        f"{URL}/views?view={pk}&q=Planned&filter=", data={"csrf_token": token, "name": "Renamed"}
    )
    assert update.status_code == 303
    loaded = await client.get(update.headers["location"])
    assert "Renamed" in loaded.text
    assert "Planned" in loaded.text
    assert (
        await client.post(f"{URL}/views/{pk}/delete", data={"csrf_token": token})
    ).status_code == 303
    assert (await client.get(location)).status_code == 404


async def test_saved_views_are_private_and_scoped_to_model_and_site(
    client: httpx.AsyncClient,
) -> None:
    token = await signed_in(client)
    response = await client.post(f"{URL}/views", data={"csrf_token": token, "name": "Private view"})
    location = response.headers["location"]
    pk = int(location.split("=")[-1])
    await signed_in(client, "bob")
    assert "Private view" not in (await client.get(URL)).text
    assert (await client.get(location)).status_code == 404
    bob_token = await csrf_for("bob")
    assert (
        await client.post(
            f"{URL}/views?view={pk}", data={"csrf_token": bob_token, "name": "Stolen"}
        )
    ).status_code == 404
    assert (
        await client.post(f"{URL}/views/{pk}/delete", data={"csrf_token": bob_token})
    ).status_code == 404
    await login_session(client, "alice")
    assert (await client.get(f"/admin/shop/order?view={pk}")).status_code == 404
    async with session_scope() as db:
        saved = await db.get(SavedView, pk)
        assert saved is not None
        saved.site_name = "another-site"
    assert (await client.get(location)).status_code == 404


async def test_saved_view_save_as_new_and_validation(client: httpx.AsyncClient) -> None:
    token = await signed_in(client)
    assert (await client.post(f"{URL}/views", data={"name": "Missing CSRF"})).status_code == 403
    assert (
        await client.post(f"{URL}/views", data={"name": " ", "csrf_token": token})
    ).status_code == 400
    assert (
        await client.post(f"{URL}/views?sort=missing", data={"name": "Bad", "csrf_token": token})
    ).status_code == 400
    saved = await client.post(f"{URL}/views", data={"name": "Original", "csrf_token": token})
    pk = saved.headers["location"].split("=")[-1]
    copied = await client.post(
        f"{URL}/views?view={pk}", data={"name": "Copy", "save_as": "new", "csrf_token": token}
    )
    assert copied.status_code == 303
    assert copied.headers["location"] != saved.headers["location"]
    async with session_scope() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(SavedView)) == 2


async def test_saved_view_permissions_rechecked_after_saving(client: httpx.AsyncClient) -> None:
    await make_user("viewer")
    await grant_permissions("viewer", "shop.product.read")
    await login_session(client, "viewer")
    token = await csrf_for("viewer")
    saved = await client.post(f"{URL}/views", data={"name": "Mine", "csrf_token": token})
    assert saved.status_code == 303
    from obox_builder.contrib.auth.models import Role

    async with session_scope() as db:
        role = await db.scalar(sa.select(Role).where(Role.name == "viewer-role"))
        assert role is not None
        await db.refresh(role, ["permissions"])
        role.permissions.clear()
    assert (await client.get(saved.headers["location"])).status_code == 403


@pytest.mark.parametrize("query", ["dien thoai", "ĐIỆN THOẠI", "điện thoại"])
async def test_global_search_vietnamese_and_permission_filter(
    client: httpx.AsyncClient, query: str
) -> None:
    pk = await make_product("Điện thoại tốt")
    async with session_scope() as db:
        db.add(Order(reference="Điện thoại restricted"))
    await make_user("viewer")
    await grant_permissions("viewer", "shop.product.read")
    await login_session(client, "viewer")
    response = await client.get("/admin/search", params={"q": query})
    assert response.status_code == 200
    assert "Điện thoại tốt" in response.text
    assert "Điện thoại restricted" not in response.text
    assert "<mark>Điện thoại</mark>" in response.text
    assert f'href="{URL}/{pk}"' in response.text


async def test_global_search_scopes_rows_and_checks_object_permission(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await signed_in(client)
    for name, active in [("Match public", True), ("Match filtered", False), ("Match denied", True)]:
        await make_product(name, is_active=active)
    original = ProductAdmin.get_queryset
    monkeypatch.setattr(
        ProductAdmin, "get_queryset", lambda self, ctx: original(self, ctx).where(Product.is_active)
    )
    monkeypatch.setattr(
        ProductAdmin,
        "has_view_permission",
        lambda self, ctx, obj=None: obj is None or obj.name != "Match denied",
    )
    response = await client.get("/admin/search", params={"q": "Match"})
    assert response.status_code == 200
    assert "Match public" in response.text
    assert "Match filtered" not in response.text
    assert "Match denied" not in response.text


async def test_global_search_empty_wildcards_unicode_and_html(client: httpx.AsyncClient) -> None:
    import unicodedata

    await signed_in(client)
    await make_product(unicodedata.normalize("NFD", "Đường phố"))
    await make_product('<script>alert("x")</script> 100%_safe')
    await make_product("Other text")
    assert (await client.get("/admin/search")).status_code == 200
    unicode = await client.get("/admin/search", params={"q": "duong pho"})
    assert unicode.status_code == 200
    assert "Other text" not in unicode.text
    assert "<mark>" in unicode.text
    wildcard = await client.get("/admin/search", params={"q": "%_"})
    assert wildcard.status_code == 200
    assert "100" in wildcard.text
    assert "Other text" not in wildcard.text
    assert '<script>alert("x")</script>' not in wildcard.text
    assert "&lt;script&gt;" in wildcard.text
    assert (await client.get("/admin/search", params={"q": "x" * 201})).status_code == 400


async def test_global_search_requires_auth_and_limits_per_model(client: httpx.AsyncClient) -> None:
    assert (await client.get("/admin/search", headers={"accept": "text/html"})).status_code == 303
    await signed_in(client)
    for index in range(25):
        await make_product(f"Match {index}")
    async with session_scope() as db:
        db.add(Order(reference="Match order"))
    response = await client.get("/admin/search", params={"q": "Match"})
    assert response.status_code == 200
    assert response.text.count('class="list-group-item list-group-item-action"') == 21
    assert "Match order" in response.text


async def test_saved_legacy_filters_can_be_removed_and_empty_sort_is_preserved(
    client: httpx.AsyncClient,
) -> None:
    token = await signed_in(client)
    await make_product("Published", status="live")
    await make_product("Draft", status="draft")
    saved = await client.post(
        f"{URL}/views?f=status:eq:live&sort=",
        data={"csrf_token": token, "name": "Legacy"},
    )
    pk = int(saved.headers["location"].split("=")[-1])
    async with session_scope() as db:
        view = await db.get(SavedView, pk)
        assert view is not None
        assert "f" not in view.config
        assert view.config["sort"] == ""
    cleared = await client.post(
        f"{URL}/views",
        params={"view": pk, "filter": json.dumps(tree("and"))},
        data={"csrf_token": token, "name": "All"},
    )
    response = await client.get(cleared.headers["location"])
    assert response.status_code == 200
    assert "Published" in response.text
    assert "Draft" in response.text


async def test_saved_views_reject_stale_fields_and_custom_permission_denial(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await signed_in(client)
    saved = await client.post(f"{URL}/views", data={"csrf_token": token, "name": "Mine"})
    pk = int(saved.headers["location"].split("=")[-1])
    async with session_scope() as db:
        view = await db.get(SavedView, pk)
        assert view is not None
        view.config = {"sort": "removed_field"}
    assert (await client.get(saved.headers["location"])).status_code == 400
    monkeypatch.setattr(ProductAdmin, "has_view_permission", lambda self, ctx, obj=None: False)
    assert (await client.get(URL)).status_code == 403
    assert (
        await client.post(f"{URL}/views", data={"csrf_token": token, "name": "Denied"})
    ).status_code == 403
    assert (
        await client.post(f"{URL}/views/{pk}/delete", data={"csrf_token": token})
    ).status_code == 403


async def test_global_search_does_not_query_secret_fields(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    await signed_in(client)
    await make_product("Private needle")
    original = ProductAdmin.get_search_specs
    monkeypatch.setattr(
        ProductAdmin,
        "get_search_specs",
        lambda self: [replace(spec, options={"secret": True}) for spec in original(self)],
    )
    response = await client.get("/admin/search", params={"q": "needle"})
    assert response.status_code == 200
    assert "Private needle" not in response.text
    assert "No matches found." in response.text


async def test_sort_links_reset_page_and_preserve_filters(client: httpx.AsyncClient) -> None:
    from html import unescape
    from urllib.parse import parse_qs, urlsplit

    await signed_in(client)
    response = await client.get(URL, params={"f": "status:eq:live", "sort": "name", "page": 2})
    links = re.findall(r'href="([^"]+)"', response.text)
    sort_link = next(link for link in links if "sort=-name" in link)
    params = parse_qs(urlsplit(unescape(sort_link)).query)
    assert "page" not in params
    assert params["f"] == ["status:eq:live"]


async def test_global_search_sql_metacharacters_are_literal(client: httpx.AsyncClient) -> None:
    await signed_in(client)
    await make_product("Ordinary")
    response = await client.get("/admin/search", params={"q": "' OR 1=1 --"})
    assert response.status_code == 200
    assert "Ordinary" not in response.text


async def test_global_search_highlights_matches_beyond_the_cell_preview(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from obox_builder.fields import field_types

    await signed_in(client)
    await make_product("Prefix " * 15 + "needle at the end")
    original = ProductAdmin.get_search_specs
    monkeypatch.setattr(
        ProductAdmin,
        "get_search_specs",
        lambda self: [
            replace(spec, type=field_types.create("long_text")) for spec in original(self)
        ],
    )
    response = await client.get("/admin/search", params={"q": "needle"})
    assert response.status_code == 200
    assert "<mark>needle</mark>" in response.text


async def test_vietnamese_sort_group_and_saved_view(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from html import unescape

    from obox_builder.conf import settings

    pytest.importorskip("icu", reason="requires the vietnamese extra")
    monkeypatch.setattr(settings, "ADMIN_TEXT_COLLATION", "vi")
    token = await signed_in(client)
    names = ["An", "Ăn", "Ân", "Ba", "Da", "Đa", "Em"]
    for name in reversed(names):
        await make_product(name)
    saved = await client.post(
        f"{URL}/views",
        params={"group": "name", "sort": "name", "size": 3},
        data={"csrf_token": token, "name": "Vietnamese"},
    )
    assert saved.status_code == 303
    location = saved.headers["location"]
    found: list[str] = []
    for page in range(1, 4):
        listing = await client.get(location + f"&page={page}")
        assert listing.status_code == 200
        labels = re.findall(r'aria-label="Select ([^"]+)"', listing.text)
        found.extend(unescape(label) for label in labels if label != "all on this page")
    assert found == names
