from __future__ import annotations

import datetime as dt
import math
from dataclasses import replace
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.contrib.admin import SimpleListFilter
from obox_builder.contrib.admin.summaries import summary_choices
from obox_builder.db.session import session_scope
from obox_builder.fields import FieldSpec, field_types

from tests.admin.conftest import grant_permissions, login_session, make_user
from tests.support.apps.shop.admin import ProductAdmin
from tests.support.apps.shop.models import Product

pytestmark = pytest.mark.usefixtures("admin_env")
URL = "/admin/shop/product/summary"


@pytest.fixture
async def summary_client(client: httpx.AsyncClient) -> httpx.AsyncClient:
    await make_user("summary-user", is_superuser=True)
    await login_session(client, "summary-user")
    async with session_scope() as db:
        db.add_all(
            [
                Product(name=name, price_cents=price, is_active=active, status=status)
                for name, price, active, status in [
                    ("Alpha", 10, True, "live"),
                    ("", 20, False, "live"),
                    ("Alpha", 30, True, "live"),
                    ("Beta", 50, False, "draft"),
                ]
            ]
        )
    return client


@pytest.mark.parametrize(
    ("field", "operation", "expected"),
    [
        ("price_cents", "sum", 110),
        ("price_cents", "average", 27.5),
        ("price_cents", "median", 25),
        ("price_cents", "min", 10),
        ("price_cents", "max", 50),
        ("price_cents", "range", 40),
        ("price_cents", "stddev", math.sqrt(218.75)),
        ("name", "empty", 1),
        ("name", "filled", 3),
        ("name", "unique", 2),
        ("name", "percent_empty", 25),
        ("name", "percent_filled", 75),
        ("name", "percent_unique", 50),
        ("category_id", "empty", 4),
        ("is_active", "checked", 2),
        ("is_active", "unchecked", 2),
        ("is_active", "percent_checked", 50),
        ("is_active", "filled", 4),
    ],
)
@pytest.mark.parametrize("object_permissions", [False, True])
async def test_summaries_cover_all_pages(
    summary_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    operation: str,
    expected: float,
    object_permissions: bool,
) -> None:
    if object_permissions:
        monkeypatch.setattr(ProductAdmin, "has_view_permission", lambda self, ctx, obj=None: True)
    response = await summary_client.get(
        URL,
        params={
            "field": field,
            "operation": operation,
            "size": 1,
            "page": 3,
        },
    )
    assert response.status_code == 200, response.text
    assert float(response.json()["value"]) == pytest.approx(expected)
    assert response.headers["cache-control"] == "private, no-store"


async def test_summary_respects_filters_scope_and_object_permissions(
    summary_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ActiveFilter(SimpleListFilter):
        title = "Activity"
        parameter_name = "active_only"

        def lookups(self) -> list[tuple[str, str]]:
            return [("yes", "Yes")]

        def queryset(self, queryset: Any, value: str) -> Any:
            return queryset.where(Product.is_active.is_(True))

    monkeypatch.setattr(ProductAdmin, "list_filter", (ActiveFilter,))
    monkeypatch.setattr(
        ProductAdmin,
        "get_queryset",
        lambda self, ctx: sa.select(Product).where(Product.price_cents < 50),
    )
    response = await summary_client.get(
        URL,
        params={
            "field": "price_cents",
            "operation": "sum",
            "active_only": "yes",
            "f": "status:eq:live",
            "q": "Alpha",
        },
    )
    assert response.json()["value"] == 40
    monkeypatch.setattr(
        ProductAdmin,
        "has_view_permission",
        lambda self, ctx, obj=None: obj is None or obj.price_cents < 30,
    )
    response = await summary_client.get(URL, params={"field": "price_cents", "operation": "sum"})
    assert response.json()["value"] == 30


async def test_summary_deduplicates_joined_records_and_handles_empty_queries(
    summary_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate_ids = sa.union_all(sa.select(Product.id), sa.select(Product.id)).subquery()
    monkeypatch.setattr(
        ProductAdmin,
        "get_queryset",
        lambda self, ctx: sa.select(Product).join(duplicate_ids, Product.id == duplicate_ids.c.id),
    )
    for operation, expected in [("sum", 110), ("median", 25)]:
        response = await summary_client.get(
            URL, params={"field": "price_cents", "operation": operation}
        )
        assert float(response.json()["value"]) == expected
    for operation, empty_expected in [
        ("sum", 0),
        ("average", None),
        ("median", None),
        ("stddev", None),
        ("percent_filled", 0),
    ]:
        response = await summary_client.get(
            URL, params={"field": "price_cents", "operation": operation, "q": "missing"}
        )
        assert response.json()["value"] == empty_expected


async def test_date_summary_and_odd_median(
    summary_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ProductAdmin, "list_display", ("created_at", "price_cents"))
    async with session_scope() as db:
        for index, product in enumerate(
            (await db.scalars(sa.select(Product).order_by(Product.id))).all()
        ):
            product.created_at = dt.datetime(2026, 1, index + 1, tzinfo=dt.timezone.utc)
    response = await summary_client.get(URL, params={"field": "created_at", "operation": "min"})
    assert response.json()["value"].startswith("2026-01-01")
    response = await summary_client.get(
        URL, params={"field": "price_cents", "operation": "median", "f": "status:eq:live"}
    )
    assert float(response.json()["value"]) == 20


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("sum", 1.1),
        ("average", 0.275),
        ("median", 0.25),
        ("range", 0.4),
        ("stddev", math.sqrt(0.021875)),
    ],
)
async def test_decimal_summaries(
    summary_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    expected: float,
) -> None:
    spec = FieldSpec(
        name="amount",
        label="Amount",
        type=field_types.create("decimal"),
        column=sa.cast(Product.price_cents, sa.Numeric(20, 4)) / 100,
    )
    monkeypatch.setattr(ProductAdmin, "get_list_specs", lambda self, ctx: [spec])
    response = await summary_client.get(URL, params={"field": "amount", "operation": operation})
    assert float(response.json()["value"]) == pytest.approx(expected)


async def test_numeric_summaries_skip_nulls_and_count_zero_as_filled(
    summary_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = FieldSpec(
        name="amount",
        label="Amount",
        type=field_types.create("integer"),
        column=sa.case((Product.price_cents == 20, None), else_=Product.price_cents - 10),
    )
    monkeypatch.setattr(ProductAdmin, "get_list_specs", lambda self, ctx: [spec])
    for operation, expected in [("filled", 3), ("empty", 1), ("sum", 60), ("median", 20)]:
        response = await summary_client.get(URL, params={"field": "amount", "operation": operation})
        assert float(response.json()["value"]) == expected


@pytest.mark.parametrize(
    ("field", "operation"),
    [
        ("name", "sum"),
        ("price_cents", "invalid"),
        ("id", "sum"),
        ("meta", "filled"),
        ("missing", "unique"),
    ],
)
async def test_summary_rejects_unsupported_fields_and_operations(
    summary_client: httpx.AsyncClient, field: str, operation: str
) -> None:
    assert (
        await summary_client.get(URL, params={"field": field, "operation": operation})
    ).status_code == 400


async def test_summary_permissions_and_private_views(client: httpx.AsyncClient) -> None:
    assert (await client.get(URL)).status_code == 401
    await make_user("reader")
    await login_session(client, "reader")
    params = {"field": "name", "operation": "filled"}
    assert (await client.get(URL, params=params)).status_code == 403
    await grant_permissions("reader", "shop.view_product")
    assert (await client.get(URL, params={**params, "view": "999"})).status_code == 404
    assert (await client.get(URL, params={**params, "f": "missing:eq:1"})).status_code == 400


def test_summary_suggestions_exclude_secrets_and_computed_fields() -> None:
    spec = FieldSpec(
        name="value",
        label="Value",
        type=field_types.create("decimal"),
        column=sa.column("value", sa.Numeric),
    )
    assert "sum" in dict(summary_choices(spec))
    assert "sum" not in dict(summary_choices(replace(spec, primary_key=True)))
    for options in ({"secret": True}, {"computed": True}, {"relationship_path": ("other",)}):
        assert summary_choices(replace(spec, options=options)) == ()
    assert summary_choices(replace(spec, type=field_types.create("json"))) == ()
