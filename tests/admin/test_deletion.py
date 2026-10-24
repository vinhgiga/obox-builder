from __future__ import annotations

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.contrib.admin import ModelAdmin, site
from obox_builder.db.session import session_scope

from tests.admin.conftest import (
    csrf_for,
    deletion_confirmation,
    login_session,
)
from tests.admin.test_permissions import signed_in
from tests.support.apps.shop.models import Invoice, Label, Order, OrderLine, Shipment, Tracking

URL = "/admin/shop/order"


async def graph(*, protected: bool = False) -> tuple[int, int, int]:
    async with session_scope() as db:
        label = Label(id="label", title="Keep this label")
        order = Order(
            reference="Delete order", labels=[label], lines=[OrderLine(sku="Cascade line")]
        )
        db.add(order)
        await db.flush()
        shipment = Shipment(order_id=order.id, name="Cascade shipment")
        db.add(shipment)
        await db.flush()
        tracking = Tracking(shipment_id=shipment.id, name="Keep this tracking")
        db.add(tracking)
        if protected:
            db.add(Invoice(order_id=order.id, name="Protected invoice"))
        await db.flush()
        return order.id, shipment.id, tracking.id


async def test_preview_describes_cascades_unlinks_and_set_null(
    relation_client: httpx.AsyncClient,
) -> None:
    client = relation_client
    token = await signed_in(client)
    order, shipment, tracking = await graph()
    preview = await client.get(f"{URL}/{order}/delete")
    assert preview.status_code == 200
    for text in (
        "Cascade line",
        "Cascade shipment",
        "Keep this tracking",
        "Remove link",
        "Clear link",
        "Confirm deletion",
    ):
        assert text in preview.text
    async with session_scope() as db:
        assert await db.get(Order, order) is not None
    # A direct POST only shows the preview.
    first = await client.post(f"{URL}/{order}/delete", data={"csrf_token": token})
    assert first.status_code == 200
    async with session_scope() as db:
        assert await db.get(Order, order) is not None
    result = await client.post(
        f"{URL}/{order}/delete",
        data={
            "csrf_token": token,
            "_delete_confirmation": deletion_confirmation(preview),
        },
    )
    assert result.status_code == 303
    async with session_scope() as db:
        assert await db.get(Order, order) is None
        assert await db.get(Shipment, shipment) is None
        assert await db.scalar(sa.select(OrderLine).where(OrderLine.order_id == order)) is None
        row = await db.get(Tracking, tracking)
        assert row is not None
        assert row.shipment_id is None
        assert await db.get(Label, "label") is not None


async def test_protected_graph_cannot_be_deleted(relation_client: httpx.AsyncClient) -> None:
    client = relation_client
    token = await signed_in(client)
    order, _, _ = await graph(protected=True)
    preview = await client.get(f"{URL}/{order}/delete")
    assert "Protected invoice" in preview.text
    assert "prevents deletion" in preview.text
    result = await client.post(
        f"{URL}/{order}/delete",
        data={
            "csrf_token": token,
            "_delete_confirmation": deletion_confirmation(preview),
        },
    )
    assert result.status_code == 409
    async with session_scope() as db:
        assert await db.get(Order, order) is not None


@pytest.mark.parametrize("restriction", ["delete", "change", "queryset"])
async def test_related_permissions_and_scope_block_deletion_without_leaking_labels(
    relation_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    restriction: str,
) -> None:
    client = relation_client
    token = await signed_in(client)
    order, _, _ = await graph()
    model = Tracking if restriction == "change" else Shipment
    admin = site.get_model_admin_for(model)
    assert admin is not None
    if restriction == "queryset":
        monkeypatch.setattr(
            admin, "get_queryset", lambda ctx: ModelAdmin.get_queryset(admin, ctx).where(sa.false())
        )
    else:
        monkeypatch.setattr(admin, f"has_{restriction}_permission", lambda *args: False)
        monkeypatch.setattr(admin, "has_view_permission", lambda *args: False)
    preview = await client.get(f"{URL}/{order}/delete")
    assert "Additional permissions required" in preview.text
    assert ("Keep this tracking" if model is Tracking else "Cascade shipment") not in preview.text
    result = await client.post(
        f"{URL}/{order}/delete",
        data={
            "csrf_token": token,
            "_delete_confirmation": deletion_confirmation(preview),
        },
    )
    assert result.status_code == 403
    async with session_scope() as db:
        assert await db.get(Order, order) is not None


@pytest.mark.parametrize("change", ["related", "root", "tamper", "session"])
async def test_confirmation_is_bound_to_impact_and_session(
    relation_client: httpx.AsyncClient,
    change: str,
) -> None:
    client = relation_client
    token = await signed_in(client)
    order, _, _ = await graph()
    preview = await client.get(f"{URL}/{order}/delete")
    confirmation = deletion_confirmation(preview)
    if change == "related":
        async with session_scope() as db:
            db.add(Shipment(order_id=order, name="Added after preview"))
    elif change == "root":
        async with session_scope() as db:
            row = await db.get(Order, order)
            assert row is not None
            row.reference = "Renamed"
    elif change == "tamper":
        confirmation += "bad"
    else:
        await login_session(client, "operator")
        token = await csrf_for("operator")
    result = await client.post(
        f"{URL}/{order}/delete", data={"csrf_token": token, "_delete_confirmation": confirmation}
    )
    assert result.status_code == 409
    assert "Review the deletion again" in result.text
    async with session_scope() as db:
        assert await db.get(Order, order) is not None


async def test_bulk_preview_freezes_selection_and_checks_all_dependencies(
    relation_client: httpx.AsyncClient,
) -> None:
    client = relation_client
    token = await signed_in(client)
    order, _, _ = await graph()
    async with session_scope() as db:
        other = Order(reference="Keep order")
        db.add(other)
        await db.flush()
        other_id = other.id
    data = {"csrf_token": token, "action": "delete_selected", "_selected_action": str(order)}
    preview = await client.post(f"{URL}/actions", data=data)
    assert preview.status_code == 200
    assert 'name="select_across" value="0"' in preview.text
    result = await client.post(
        f"{URL}/actions", data={**data, "_delete_confirmation": deletion_confirmation(preview)}
    )
    assert result.status_code == 303
    async with session_scope() as db:
        assert await db.get(Order, order) is None
        assert await db.get(Order, other_id) is not None


async def test_expired_confirmation_requires_another_review(
    relation_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    from obox_builder.contrib.admin import deletion

    client = relation_client
    token = await signed_in(client)
    order, _, _ = await graph()
    preview = await client.get(f"{URL}/{order}/delete")
    expired = time.time() + deletion.CONFIRM_SECONDS + 1
    monkeypatch.setattr(deletion.time, "time", lambda: expired)
    response = await client.post(
        f"{URL}/{order}/delete",
        data={
            "csrf_token": token,
            "_delete_confirmation": deletion_confirmation(preview),
        },
    )
    assert response.status_code == 409
    async with session_scope() as db:
        assert await db.get(Order, order) is not None
