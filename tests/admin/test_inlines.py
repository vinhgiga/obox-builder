from __future__ import annotations

from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.contrib.admin import InlineFormSet, ModelAdmin, StackedInline, site
from obox_builder.db.session import session_scope

from tests.admin.test_permissions import signed_in
from tests.support.apps.shop.models import Order, OrderLine

URL = "/admin/shop/order"


def payload(token: str, reference: str = "Order", *rows: dict[str, Any]) -> dict[str, Any]:
    data = {"csrf_token": token, "reference": reference, "inline:0:_total": str(len(rows))}
    for index, row in enumerate(rows):
        data.update({f"inline:0:{index}:{key}": value for key, value in row.items()})
    return data


async def create_order(reference: str = "Order", *skus: str) -> int:
    async with session_scope() as db:
        order = Order(reference=reference, lines=[OrderLine(sku=sku) for sku in skus])
        db.add(order)
        await db.flush()
        return order.id


async def lines_of(pk: int) -> list[OrderLine]:
    async with session_scope() as db:
        return list(
            await db.scalars(
                sa.select(OrderLine).where(OrderLine.order_id == pk).order_by(OrderLine.id)
            )
        )


async def test_inline_create_update_delete_and_empty_extra(
    relation_client: httpx.AsyncClient,
) -> None:
    client = relation_client
    token = await signed_in(client)
    form = await client.get(f"{URL}/add")
    assert "data-inline-formset" in form.text
    assert 'name="inline:0:0:order_id"' not in form.text
    response = await client.post(
        f"{URL}/add",
        data=payload(token, "New", {"sku": "A", "quantity": "2"}, {"sku": "", "quantity": "1"}),
    )
    assert response.status_code == 303
    async with session_scope() as db:
        pk = await db.scalar(sa.select(Order.id).where(Order.reference == "New"))
    assert pk is not None
    lines = await lines_of(pk)
    assert [(line.sku, line.quantity) for line in lines] == [("A", 2)]
    response = await client.post(
        f"{URL}/{pk}",
        data=payload(
            token,
            "Changed",
            {"_pk": str(lines[0].id), "sku": "A2", "quantity": "4", "order_id": "999"},
            {"sku": "B", "quantity": "1"},
        ),
    )
    assert response.status_code == 303
    lines = await lines_of(pk)
    assert [(line.sku, line.quantity) for line in lines] == [("A2", 4), ("B", 1)]
    response = await client.post(
        f"{URL}/{pk}", data=payload(token, "Changed", {"_pk": str(lines[0].id), "_delete": "1"})
    )
    assert response.status_code == 303
    assert [line.sku for line in await lines_of(pk)] == ["B"]


@pytest.mark.parametrize("failure", ["required", "type", "duplicate", "count", "limit"])
async def test_inline_errors_leave_parent_and_children_unchanged(
    relation_client: httpx.AsyncClient,
    failure: str,
) -> None:
    client = relation_client
    token = await signed_in(client)
    pk = await create_order("Original", "A")
    row = (
        {"sku": "", "quantity": "1"}
        if failure == "required"
        else {"sku": "A", "quantity": "bad" if failure == "type" else "2"}
    )
    # An empty added row is intentionally ignored; a changed quantity makes it nonempty.
    if failure == "required":
        row["quantity"] = "2"
    data = payload(token, "Changed", row)
    if failure == "count":
        data["inline:0:_total"] = "-1"
    if failure == "limit":
        data["inline:0:_total"] = "1001"
    response = await client.post(f"{URL}/{pk}", data=data)
    assert response.status_code == 200
    assert "Changed" in response.text
    async with session_scope() as db:
        parent = await db.get(Order, pk)
        assert parent is not None
        assert parent.reference == "Original"
    assert [(line.sku, line.quantity) for line in await lines_of(pk)] == [("A", 1)]


@pytest.mark.parametrize(
    "attack", ["other_parent", "duplicate", "queryset", "change", "delete", "add"]
)
async def test_inline_rejects_forged_or_unauthorized_rows(
    relation_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    client = relation_client
    token = await signed_in(client)
    pk = await create_order("Original", "A")
    other = await create_order("Other", "B")
    line = (await lines_of(other if attack == "other_parent" else pk))[0]
    child = site.get_model_admin_for(OrderLine)
    assert child is not None
    if attack == "queryset":
        monkeypatch.setattr(
            child, "get_queryset", lambda ctx: ModelAdmin.get_queryset(child, ctx).where(sa.false())
        )
    if attack in {"change", "delete", "add"}:
        monkeypatch.setattr(child, f"has_{attack}_permission", lambda *args: False)
    row = {"_pk": str(line.id), "sku": "Changed", "quantity": "5"}
    if attack == "add":
        row.pop("_pk")
    if attack == "delete":
        row["_delete"] = "1"
    data = payload(token, "Changed", row, *([row] if attack == "duplicate" else []))
    response = await client.post(f"{URL}/{pk}", data=data)
    assert response.status_code == 403
    assert [item.sku for item in await lines_of(pk)] == ["A"]
    assert [item.sku for item in await lines_of(other)] == ["B"]


async def test_inline_limits_and_stacked_layout(
    relation_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = relation_client
    token = await signed_in(client)
    parent = site.get_model_admin_for(Order)
    assert parent is not None
    base = parent.inlines[0]

    limited_inline = type(
        "LimitedInline",
        (base,),
        {"min_num": 1, "max_num": 1, "extra": 1, "layout": StackedInline.layout},
    )

    monkeypatch.setattr(parent, "inlines", (limited_inline,))
    assert (await client.get(f"{URL}/add")).status_code == 200
    response = await client.post(f"{URL}/add", data=payload(token, "Too few"))
    assert "Keep between 1 and 1" in response.text
    response = await client.post(
        f"{URL}/add",
        data=payload(
            token, "Too many", {"sku": "A", "quantity": "1"}, {"sku": "B", "quantity": "1"}
        ),
    )
    assert "Keep between 1 and 1" in response.text
    async with session_scope() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(Order)) == 0


async def test_custom_formset_validation_checks_all_child_rows(
    relation_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = relation_client
    token = await signed_in(client)
    parent = site.get_model_admin_for(Order)
    assert parent is not None
    base = parent.inlines[0]

    class UniqueSkuFormSet(InlineFormSet):
        async def clean(self) -> None:
            skus = [
                row.bound.cleaned["sku"]
                for row in self.rows
                if not row.delete and row.bound is not None and row.bound.is_valid
            ]
            if len(skus) != len(set(skus)):
                self.errors.append("Each order line must have a unique SKU.")

    unique_inline = type(
        "UniqueSkuInline",
        (base,),
        {"formset_class": UniqueSkuFormSet},
    )
    monkeypatch.setattr(parent, "inlines", (unique_inline,))

    response = await client.post(
        f"{URL}/add",
        data=payload(
            token,
            "Duplicate lines",
            {"sku": "A", "quantity": "1"},
            {"sku": "A", "quantity": "2"},
        ),
    )

    assert response.status_code == 200
    assert "Each order line must have a unique SKU." in response.text
    async with session_scope() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(Order)) == 0


async def test_inline_can_link_to_the_registered_child_admin(
    relation_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = relation_client
    order_id = await create_order("Detailed", "A")
    line = (await lines_of(order_id))[0]
    parent = site.get_model_admin_for(Order)
    child = site.get_model_admin_for(OrderLine)
    assert parent is not None
    assert child is not None
    base = parent.inlines[0]
    linked_inline = type("LinkedInline", (base,), {"show_change_link": True})
    monkeypatch.setattr(parent, "inlines", (linked_inline,))
    await signed_in(client)

    response = await client.get(f"{URL}/{order_id}")

    assert response.status_code == 200
    assert f'href="{child.change_path(line.id)}">Edit details</a>' in response.text


async def test_inline_failure_rolls_back_entire_batch(relation_client: httpx.AsyncClient) -> None:
    client = relation_client
    token = await signed_in(client)
    pk = await create_order("Original", "A", "B")
    first, second = await lines_of(pk)
    response = await client.post(
        f"{URL}/{pk}",
        data=payload(
            token,
            "Changed",
            {"_pk": str(first.id), "sku": "A2", "quantity": "2"},
            {"_pk": str(second.id), "sku": "A2", "quantity": "3"},
        ),
    )
    assert response.status_code == 200
    assert "database rejected" in response.text
    assert [line.sku for line in await lines_of(pk)] == ["A", "B"]


async def test_view_only_parent_and_children_have_no_edit_controls(
    relation_client: httpx.AsyncClient,
) -> None:
    from tests.admin.conftest import grant_permissions, login_session, make_user

    client = relation_client
    pk = await create_order("Readonly", "Readonly line")
    await make_user("reader")
    await grant_permissions("reader", "shop.view_order", "shop.view_order_line")
    await login_session(client, "reader")
    response = await client.get(f"{URL}/{pk}")
    assert response.status_code == 200
    assert "Readonly line" in response.text
    assert 'name="inline:0:0:sku"' not in response.text
    assert "data-inline-add" not in response.text
    assert 'name="_action"' not in response.text
