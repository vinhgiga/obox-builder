from __future__ import annotations

import csv
import io
import json

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.contrib.savedviews.models import SavedView
from obox_builder.contrib.admin.views import exports
from obox_builder.db.session import session_scope

from tests.admin.conftest import csrf_for, grant_permissions, login_session, make_user
from tests.admin.test_model_views import make_product
from tests.support.apps.shop.admin import ProductAdmin
from tests.support.apps.shop.models import Product

pytestmark = pytest.mark.usefixtures("admin_env")
URL = "/admin/shop/product"


async def saved_view(client: httpx.AsyncClient, name: str = "My view") -> tuple[str, int]:
    await make_user("view-owner", is_superuser=True)
    await login_session(client, "view-owner")
    token = await csrf_for("view-owner")
    response = await client.post(
        f"{URL}/views?f=status:eq:live&sort=name&group=status&page=3",
        data={"csrf_token": token, "name": name},
    )
    assert response.status_code == 303
    return token, int(response.headers["location"].split("=")[-1])


async def test_view_rename_changes_only_name_and_duplicate_preserves_config(
    client: httpx.AsyncClient,
) -> None:
    token, pk = await saved_view(client)
    async with session_scope() as db:
        original = await db.get(SavedView, pk)
        assert original is not None
        config = dict(original.config)
    response = await client.post(
        f"{URL}/views/{pk}/rename?q=changed&sort=-id",
        data={"csrf_token": token, "name": "  Renamed  "},
    )
    assert response.status_code == 303
    assert "q=changed" in response.headers["location"]
    assert "sort=-id" in response.headers["location"]
    copied = await client.post(f"{URL}/views/{pk}/duplicate", data={"csrf_token": token})
    copy_pk = int(copied.headers["location"].split("=")[-1])
    assert copy_pk != pk
    async with session_scope() as db:
        original = await db.get(SavedView, pk)
        duplicate = await db.get(SavedView, copy_pk)
        assert original is not None
        assert duplicate is not None
        assert original.name == "Renamed"
        assert duplicate.name == "Renamed copy"
        assert original.config == duplicate.config == config
        assert original.owner_id == duplicate.owner_id
    deleted = await client.post(f"{URL}/views/{copy_pk}/delete", data={"csrf_token": token})
    assert deleted.status_code == 303
    assert (await client.get(f"{URL}?view={pk}")).status_code == 200


@pytest.mark.parametrize("name", ["", " ", "a" * 101])
async def test_view_rename_validates_names(client: httpx.AsyncClient, name: str) -> None:
    token, pk = await saved_view(client)
    response = await client.post(
        f"{URL}/views/{pk}/rename", data={"csrf_token": token, "name": name}
    )
    assert response.status_code == 400


async def test_duplicate_long_name_and_escape_view_menu(client: httpx.AsyncClient) -> None:
    token, pk = await saved_view(client, '<script>alert("view")</script>' + "a" * 70)
    response = await client.post(f"{URL}/views/{pk}/duplicate", data={"csrf_token": token})
    listing = await client.get(response.headers["location"])
    assert '<script>alert("view")</script>' not in listing.text
    assert "&lt;script&gt;" in listing.text
    async with session_scope() as db:
        names = (await db.scalars(sa.select(SavedView.name))).all()
        assert max(map(len, names)) <= 100


@pytest.mark.parametrize("operation", ["rename", "duplicate"])
async def test_view_actions_enforce_csrf_ownership_model_site_and_permissions(
    client: httpx.AsyncClient,
    operation: str,
) -> None:
    token, pk = await saved_view(client)
    path = f"{URL}/views/{pk}/{operation}"
    assert (await client.post(path, data={"name": "Bad"})).status_code == 403
    await make_user("other-owner", is_superuser=True)
    await login_session(client, "other-owner")
    other_token = await csrf_for("other-owner")
    assert (
        await client.post(path, data={"csrf_token": other_token, "name": "Bad"})
    ).status_code == 404
    await login_session(client, "view-owner")
    token = await csrf_for("view-owner")
    assert (
        await client.post(
            f"/admin/shop/order/views/{pk}/{operation}", data={"csrf_token": token, "name": "Bad"}
        )
    ).status_code == 404
    async with session_scope() as db:
        row = await db.get(SavedView, pk)
        assert row is not None
        row.site_name = "other-site"
    assert (await client.post(path, data={"csrf_token": token, "name": "Bad"})).status_code == 404
    await make_user("no-access")
    await login_session(client, "no-access")
    denied_token = await csrf_for("no-access")
    assert (
        await client.post(path, data={"csrf_token": denied_token, "name": "Bad"})
    ).status_code == 403


def rows(response: httpx.Response) -> list[list[str]]:
    assert response.status_code == 200, response.text
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["cache-control"] == "private, no-store"
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


async def test_csv_exports_all_filtered_sorted_records_and_selected_columns(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await saved_view(client)
    monkeypatch.setattr(exports, "EXPORT_BATCH_SIZE", 2)
    for name in ["Charlie", "Bravo", "Alpha", "Delta", "Echo"]:
        await make_product(name, status="live")
    await make_product("Excluded", status="draft")
    response = await client.get(
        f"{URL}/export.csv",
        params={
            "f": "status:eq:live",
            "sort": "-name",
            "size": 1,
            "page": 4,
            "columns": json.dumps(["name", "status"]),
        },
    )
    assert rows(response) == [
        ["Name", "Status"],
        ["Echo", "Live"],
        ["Delta", "Live"],
        ["Charlie", "Live"],
        ["Bravo", "Live"],
        ["Alpha", "Live"],
    ]


async def test_csv_saved_view_scope_object_permissions_and_duplicate_joins(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, pk = await saved_view(client)
    for name, price in [("Allowed", 10), ("Denied", 20), ("Scoped out", 30)]:
        await make_product(name, status="live", price_cents=price)
    await make_product("Draft", status="draft")
    duplicate_ids = sa.union_all(sa.select(Product.id), sa.select(Product.id)).subquery()
    monkeypatch.setattr(
        ProductAdmin,
        "get_queryset",
        lambda self, ctx: (
            sa.select(Product)
            .join(duplicate_ids, Product.id == duplicate_ids.c.id)
            .where(Product.price_cents < 30)
        ),
    )
    monkeypatch.setattr(
        ProductAdmin,
        "has_view_permission",
        lambda self, ctx, obj=None: obj is None or obj.price_cents != 20,
    )
    monkeypatch.setattr(exports, "EXPORT_BATCH_SIZE", 1)
    response = await client.get(f"{URL}/export.csv", params={"view": pk, "columns": '["name"]'})
    assert rows(response) == [["Name"], ["Allowed"]]


async def test_csv_escaping_unicode_empty_and_formula_cells(client: httpx.AsyncClient) -> None:
    await saved_view(client)
    names = ['Cà phê, "sữa"\nxong', "=1+1", "  @SUM(1)", "\tcmd", "normal"]
    for name in names:
        await make_product(name)
    response = await client.get(f"{URL}/export.csv?sort=id&columns=%5B%22name%22%5D")
    assert rows(response) == [
        ["Name"],
        [names[0]],
        ["'=1+1"],
        ["'  @SUM(1)"],
        ["'\tcmd"],
        ["normal"],
    ]
    assert rows(await client.get(f"{URL}/export.csv?q=missing&columns=%5B%22name%22%5D")) == [
        ["Name"]
    ]


async def test_csv_preserves_negative_numeric_values(client: httpx.AsyncClient) -> None:
    await saved_view(client)
    await make_product("Negative", price_cents=-15)
    response = await client.get(f"{URL}/export.csv", params={"columns": '["price_cents"]'})
    assert rows(response) == [["Price cents"], ["-15"]]


@pytest.mark.parametrize(
    "columns", ["[]", '["missing"]', '["name","name"]', "{}", "null", "[1]", "invalid"]
)
async def test_csv_rejects_invalid_column_selections(
    client: httpx.AsyncClient, columns: str
) -> None:
    await saved_view(client)
    assert (await client.get(f"{URL}/export.csv", params={"columns": columns})).status_code == 400


async def test_csv_checks_authentication_permissions_and_private_views(
    client: httpx.AsyncClient,
) -> None:
    assert (await client.get(f"{URL}/export.csv")).status_code == 401
    await make_user("reader")
    await login_session(client, "reader")
    assert (await client.get(f"{URL}/export.csv")).status_code == 403
    await grant_permissions("reader", "shop.product.read")
    assert (await client.get(f"{URL}/export.csv?view=999")).status_code == 404
    assert (await client.get(f"{URL}/export.csv?sort=missing")).status_code == 400
