from __future__ import annotations

from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.contrib.admin import InlineFormSet, ModelAdmin, StackedInline, site
from obox_builder.db.session import session_scope
from sqlalchemy.orm import selectinload

from tests.admin.conftest import confirmed_delete
from tests.admin.test_permissions import signed_in
from tests.support.apps.shop.models import (
    DetailedMultiTableRecord,
    Label,
    MultiTableRecord,
    Order,
    OrderDetails,
    OrderLine,
)

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


async def details_of(pk: int) -> OrderDetails | None:
    async with session_scope() as db:
        return await db.scalar(sa.select(OrderDetails).where(OrderDetails.order_id == pk))


async def test_inline_create_update_delete_and_empty_extra(
    relation_client: httpx.AsyncClient,
) -> None:
    client = relation_client
    token = await signed_in(client)
    form = await client.get(f"{URL}/add")
    assert "data-inline-formset" in form.text
    assert 'name="inline:0:0:order_id"' not in form.text
    assert 'name="inline:0:_total" value="3"' in form.text
    assert 'name="inline:1:_total" value="1"' in form.text
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


@pytest.mark.parametrize("existing", [False, True])
async def test_invalid_inline_data_is_ignored_when_marked_for_deletion(
    relation_client: httpx.AsyncClient,
    existing: bool,
) -> None:
    client = relation_client
    token = await signed_in(client)
    pk = await create_order("Original", "A") if existing else None
    line_id = (await lines_of(pk))[0].id if pk is not None else None
    row = {"sku": "", "quantity": "bad", "_delete": "1"}
    if line_id is not None:
        row["_pk"] = str(line_id)
    response = await client.post(
        f"{URL}/{pk}" if pk is not None else f"{URL}/add",
        data=payload(token, "Changed", row),
    )
    assert response.status_code == 303
    if pk is None:
        async with session_scope() as db:
            pk = await db.scalar(sa.select(Order.id).where(Order.reference == "Changed"))
        assert pk is not None
    else:
        async with session_scope() as db:
            order = await db.get(Order, pk)
            assert order is not None
            assert order.reference == "Changed"
    assert await lines_of(pk) == []


async def test_one_to_one_inline_create_update_delete_and_enforce_single_row(
    relation_client: httpx.AsyncClient,
) -> None:
    client = relation_client
    token = await signed_in(client)
    created = await client.post(
        f"{URL}/add",
        data={
            "csrf_token": token,
            "reference": "One-to-one",
            "labels": "",
            "inline:1:_total": "1",
            "inline:1:0:recipient": "Taylor",
        },
    )
    assert created.status_code == 303
    async with session_scope() as db:
        order_id = await db.scalar(sa.select(Order.id).where(Order.reference == "One-to-one"))
    assert order_id is not None
    details = await details_of(order_id)
    assert details is not None
    assert details.recipient == "Taylor"

    edit = await client.get(f"{URL}/{order_id}")
    assert 'value="Taylor"' in edit.text
    too_many = await client.post(
        f"{URL}/{order_id}",
        data={
            "csrf_token": token,
            "reference": "Must stay unchanged",
            "labels": "",
            "inline:1:_total": "2",
            "inline:1:0:_pk": str(details.id),
            "inline:1:0:recipient": "Updated",
            "inline:1:1:recipient": "Second row",
        },
    )
    assert too_many.status_code == 200
    assert "unique" in too_many.text.lower()
    current = await details_of(order_id)
    assert current is not None
    assert current.recipient == "Taylor"
    async with session_scope() as db:
        order = await db.get(Order, order_id)
        assert order is not None
        assert order.reference == "One-to-one"

    changed = await client.post(
        f"{URL}/{order_id}",
        data={
            "csrf_token": token,
            "reference": "One-to-one",
            "labels": "",
            "inline:1:_total": "1",
            "inline:1:0:_pk": str(details.id),
            "inline:1:0:recipient": "Updated",
        },
    )
    assert changed.status_code == 303
    current = await details_of(order_id)
    assert current is not None
    assert current.recipient == "Updated"

    deleted = await client.post(
        f"{URL}/{order_id}",
        data={
            "csrf_token": token,
            "reference": "One-to-one",
            "labels": "",
            "inline:1:_total": "1",
            "inline:1:0:_pk": str(details.id),
            "inline:1:0:recipient": "Updated",
            "inline:1:0:_delete": "1",
        },
    )
    assert deleted.status_code == 303
    assert await details_of(order_id) is None


async def test_save_as_new_validates_and_copies_selected_inline_rows(
    relation_client: httpx.AsyncClient,
) -> None:
    client = relation_client
    token = await signed_in(client)
    source_id = await create_order("Original", "A", "B")
    first, second = await lines_of(source_id)
    invalid = payload(
        token,
        "Invalid copy",
        {"_pk": str(first.id), "sku": "A-copy", "quantity": "bad"},
        {"_pk": str(second.id), "sku": "B-copy", "quantity": "1"},
    )
    invalid["_action"] = "save_as_new"
    invalid["labels"] = ""
    response = await client.post(f"{URL}/{source_id}", data=invalid)
    assert response.status_code == 200
    assert "Input should be a valid integer" in response.text
    async with session_scope() as db:
        assert await db.scalar(sa.select(Order.id).where(Order.reference == "Invalid copy")) is None
    assert [line.sku for line in await lines_of(source_id)] == ["A", "B"]

    copied = payload(
        token,
        "Copy",
        {"_pk": str(first.id), "sku": "A-copy", "quantity": "2"},
        {"_pk": str(second.id), "sku": "B-copy", "quantity": "1", "_delete": "1"},
    )
    copied["_action"] = "save_as_new"
    copied["labels"] = ""
    response = await client.post(f"{URL}/{source_id}", data=copied)
    assert response.status_code == 303
    async with session_scope() as db:
        copied_id = await db.scalar(sa.select(Order.id).where(Order.reference == "Copy"))
    assert copied_id is not None
    assert [(line.sku, line.quantity) for line in await lines_of(copied_id)] == [("A-copy", 2)]
    assert [line.sku for line in await lines_of(source_id)] == ["A", "B"]


async def test_joined_table_inheritance_create_change_and_delete(
    relation_client: httpx.AsyncClient,
) -> None:
    client = relation_client
    token = await signed_in(client)
    url = "/admin/shop/detailed_multi_table_record"
    created = await client.post(
        f"{url}/add",
        data={"csrf_token": token, "name": "Initial", "details": "Child data"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    async with session_scope() as db:
        record_id = await db.scalar(
            sa.select(DetailedMultiTableRecord.id).where(DetailedMultiTableRecord.name == "Initial")
        )
    assert record_id is not None
    async with session_scope() as db:
        base_id = await db.scalar(
            sa.select(MultiTableRecord.__table__.c.id).where(
                MultiTableRecord.__table__.c.id == record_id
            )
        )
        child_id = await db.scalar(
            sa.select(DetailedMultiTableRecord.__table__.c.id).where(
                DetailedMultiTableRecord.__table__.c.id == record_id
            )
        )
    assert base_id == child_id == record_id
    async with session_scope() as db:
        base = await db.get(MultiTableRecord, record_id)
        assert type(base) is MultiTableRecord

    edit = await client.get(f"{url}/{record_id}")
    assert 'value="Initial"' in edit.text
    assert 'value="Child data"' in edit.text
    changed = await client.post(
        f"{url}/{record_id}",
        data={"csrf_token": token, "name": "Changed", "details": "Updated child data"},
        follow_redirects=False,
    )
    assert changed.status_code == 303
    async with session_scope() as db:
        record = await db.get(DetailedMultiTableRecord, record_id)
        assert record is not None
        assert (record.name, record.details) == ("Changed", "Updated child data")

    deleted = await confirmed_delete(client, f"{url}/{record_id}/delete", {"csrf_token": token})
    assert deleted.status_code == 303
    async with session_scope() as db:
        assert await db.get(MultiTableRecord, record_id) is None
        assert await db.get(DetailedMultiTableRecord, record_id) is None


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
        data["inline:0:_total"] = "2001"
    response = await client.post(f"{URL}/{pk}", data=data)
    if failure == "duplicate":
        assert response.status_code == 200
        assert "unique" in response.text.lower()
    else:
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
    form = await client.get(f"{URL}/add")
    assert form.status_code == 200
    assert 'name="inline:0:_total" value="1"' in form.text
    response = await client.post(f"{URL}/add", data=payload(token, "Too few"))
    assert response.status_code == 303
    response = await client.post(
        f"{URL}/add",
        data=payload(
            token, "Too many", {"sku": "A", "quantity": "1"}, {"sku": "B", "quantity": "1"}
        ),
    )
    assert response.status_code == 303
    async with session_scope() as db:
        order_id = await db.scalar(sa.select(Order.id).where(Order.reference == "Too many"))
        assert await db.scalar(sa.select(sa.func.count()).select_from(Order)) == 2
    assert order_id is not None
    assert len(await lines_of(order_id)) == 2


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
            quantities = [
                row.bound.cleaned["quantity"]
                for row in self.rows
                if not row.delete and row.bound is not None and row.bound.is_valid
            ]
            if len(quantities) != len(set(quantities)):
                self.errors.append("Each order line must have a unique quantity.")

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
            {"sku": "B", "quantity": "1"},
        ),
    )

    assert response.status_code == 200
    assert "Each order line must have a unique quantity." in response.text
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
    async with session_scope() as db:
        db.add_all([Label(id="red", title="Red label"), Label(id="blue", title="Blue label")])
    pk = await create_order("Original", "A", "B")
    async with session_scope() as db:
        order = await db.get(Order, pk)
        red = await db.get(Label, "red")
        assert order is not None
        assert red is not None
        await db.refresh(order, ["labels"])
        order.labels = [red]
        await db.flush()
    first, second = await lines_of(pk)
    data = payload(
        token,
        "Changed",
        {"_pk": str(first.id), "sku": "A2", "quantity": "2"},
        {"_pk": str(second.id), "sku": "A2", "quantity": "3"},
    )
    data["labels"] = ["blue"]
    response = await client.post(
        f"{URL}/{pk}",
        data=data,
    )
    assert response.status_code == 200
    assert "unique" in response.text.lower()
    async with session_scope() as db:
        order = await db.get(Order, pk)
        assert order is not None
        assert order.reference == "Original"
    async with session_scope() as db:
        order = await db.scalar(
            sa.select(Order).where(Order.id == pk).options(selectinload(Order.labels))
        )
        assert order is not None
        assert [label.id for label in order.labels] == ["red"]
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
