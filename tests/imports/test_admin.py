from __future__ import annotations

import asyncio
import csv
import io
from typing import Any

import httpx
import sqlalchemy as sa
from obox_builder.contrib.admin.tasks import import_spreadsheet
from obox_builder.contrib.history import track
from obox_builder.contrib.history.models import RowChange
from obox_builder.contrib.imports.models import ImportRow, ImportSession
from obox_builder.contrib.jobs.models import Task
from obox_builder.db.session import session_scope

from tests.admin.conftest import csrf_for, grant_permissions, login_session, make_user
from tests.imports.test_parser import workbook_bytes
from tests.support.apps.shop.models import Order

BASE = "/admin/shop/order/import"
SITE = "obox_builder.contrib.admin.site.site"


async def run_phase(session_id: str, phase: str) -> None:
    async with session_scope() as db:
        session = await db.get(ImportSession, session_id)
        assert session is not None
        revision = session.revision
    await import_spreadsheet(session_id, revision, phase, SITE)


async def submit(
    client: httpx.AsyncClient, session_id: str, token: str, **data: Any
) -> httpx.Response:
    async with session_scope() as db:
        session = await db.get(ImportSession, session_id)
        assert session is not None
        revision = session.revision
    return await client.post(
        f"{BASE}/{session_id}", data={"csrf_token": token, "revision": str(revision), **data}
    )


async def upload(
    client: httpx.AsyncClient,
    *,
    mode: str = "insert",
    content: bytes = b"reference\nnew\n",
    filename: str = "data.csv",
) -> tuple[str, str]:
    await make_user("operator", is_superuser=True)
    await login_session(client, "operator")
    token = await csrf_for("operator")
    response = await client.post(
        BASE, data={"csrf_token": token, "mode": mode}, files={"file": (filename, content)}
    )
    assert response.status_code == 303, response.text
    return response.headers["location"].rsplit("/", 1)[1], token


async def preview(client: httpx.AsyncClient, session_id: str, token: str) -> None:
    await run_phase(session_id, "inspect")
    assert (
        await submit(client, session_id, token, action="parse", sheet="Data", header_row="1")
    ).status_code == 303
    await run_phase(session_id, "parse")
    assert (
        await submit(client, session_id, token, action="map", **{"column:0": "reference"})
    ).status_code == 303
    await run_phase(session_id, "validate")


async def test_full_admin_flow_and_atomic_double_confirmation(client: httpx.AsyncClient) -> None:
    track(Order)
    session_id, token = await upload(client)
    assert (await client.get(f"{BASE}/{session_id}")).status_code == 200
    await preview(client, session_id, token)
    response = await client.get(f"{BASE}/{session_id}")
    assert "Confirm import" in response.text
    assert "new" in response.text
    async with session_scope() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(Order)) == 0
        assert (
            await db.scalar(
                sa.select(sa.func.count())
                .select_from(RowChange)
                .where(RowChange.model_label == "shop.order")
            )
            == 0
        )
        session = await db.get(ImportSession, session_id)
        assert session is not None
        revision = session.revision
    payload = {"csrf_token": token, "revision": revision, "action": "commit"}
    assert (await client.post(f"{BASE}/{session_id}", data=payload)).status_code == 303
    again = await client.post(f"{BASE}/{session_id}", data=payload)
    assert "stale" in again.text
    await run_phase(session_id, "commit")
    await run_phase(session_id, "commit")
    async with session_scope() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(Order)) == 1
        entries = list(
            await db.scalars(sa.select(RowChange).where(RowChange.model_label == "shop.order"))
        )
        assert len(entries) == 1
        assert entries[0].source == "job"
        assert session_id in (entries[0].reason or "")
    assert "completed" in (await client.get(f"{BASE}/{session_id}")).text


async def test_auth_csrf_owner_isolation_and_model_permissions(client: httpx.AsyncClient) -> None:
    assert (await client.get(BASE, headers={"accept": "text/html"})).status_code == 303
    await make_user("viewer")
    await grant_permissions("viewer", "shop.order.read")
    await login_session(client, "viewer")
    assert "No import mode" in (await client.get(BASE)).text
    assert (
        await client.post(BASE, files={"file": ("data.csv", b"reference\na\n")})
    ).status_code == 403
    token = await csrf_for("viewer")
    assert (
        await client.post(
            BASE, data={"csrf_token": token}, files={"file": ("data.csv", b"reference\na\n")}
        )
    ).status_code == 403
    session_id, _ = await upload(client)
    await login_session(client, "viewer")
    assert (await client.get(f"{BASE}/{session_id}")).status_code == 404
    assert (await client.get(f"{BASE}/{session_id}/errors")).status_code == 404


async def test_error_report_original_order_and_formula_neutralization(
    client: httpx.AsyncClient,
) -> None:
    session_id, token = await upload(client, content=b"reference\n=cmd()\n=cmd()\n")
    await preview(client, session_id, token)
    report = await client.get(f"{BASE}/{session_id}/errors")
    assert report.status_code == 200
    rows = list(csv.reader(io.StringIO(report.text.lstrip("\ufeff"))))
    assert [row[0] for row in rows[1:]] == ["2", "3"]
    assert all(row[1] == "'=cmd()" and "Duplicate" in row[-1] for row in rows[1:])


async def test_cancel_invalidates_old_workers(client: httpx.AsyncClient) -> None:
    session_id, token = await upload(client)
    await preview(client, session_id, token)
    assert (await submit(client, session_id, token, action="commit")).status_code == 303
    async with session_scope() as db:
        session = await db.get(ImportSession, session_id)
        assert session is not None
        revision = session.revision
    assert (await submit(client, session_id, token, action="cancel")).status_code == 303
    await import_spreadsheet(session_id, revision, "commit", SITE)
    async with session_scope() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(Order)) == 0
        session = await db.get(ImportSession, session_id)
        assert session is not None
        assert session.status == "cancelled"


async def test_permissions_rechecked_by_worker(client: httpx.AsyncClient) -> None:
    from obox_builder.contrib.auth.models import User

    session_id, token = await upload(client)
    await preview(client, session_id, token)
    await submit(client, session_id, token, action="commit")
    async with session_scope() as db:
        await db.execute(sa.update(User).where(User.username == "operator").values(is_active=False))
    await run_phase(session_id, "commit")
    async with session_scope() as db:
        session = await db.get(ImportSession, session_id)
        assert session is not None
        assert session.status == "failed"
        assert await db.scalar(sa.select(sa.func.count()).select_from(Order)) == 0


async def test_concurrent_workers_do_not_duplicate_rows(client: httpx.AsyncClient) -> None:
    session_id, token = await upload(client, content=b"reference\na\nb\nc\n")
    await preview(client, session_id, token)
    await submit(client, session_id, token, action="commit")
    await asyncio.gather(run_phase(session_id, "commit"), run_phase(session_id, "commit"))
    async with session_scope() as db:
        assert sorted(await db.scalars(sa.select(Order.reference))) == ["a", "b", "c"]


async def test_retry_failed_job_preserves_checkpoint(client: httpx.AsyncClient) -> None:
    session_id, token = await upload(client)
    async with session_scope() as db:
        session = await db.get(ImportSession, session_id)
        assert session is not None
        await db.execute(
            sa.update(Task).where(Task.task_id == session.task_id).values(status="FAILURE")
        )
    page = await client.get(f"{BASE}/{session_id}")
    assert "Retry processing" in page.text
    assert 'http-equiv="refresh"' not in page.text
    assert (await submit(client, session_id, token, action="retry")).status_code == 303


async def test_xlsx_sheet_header_selection_and_macros_rejected(client: httpx.AsyncClient) -> None:
    session_id, token = await upload(
        client,
        filename="data.xlsx",
        content=workbook_bytes([["heading"], ["reference"], ["excel-row"]]),
    )
    await run_phase(session_id, "inspect")
    response = await submit(
        client, session_id, token, action="parse", sheet="Sheet", header_row="2"
    )
    assert response.status_code == 303
    await run_phase(session_id, "parse")
    async with session_scope() as db:
        session = await db.get(ImportSession, session_id)
        assert session is not None
        assert session.headers == ["reference"]
        row = await db.scalar(sa.select(ImportRow).where(ImportRow.session_id == session_id))
        assert row is not None
        assert row.number == 3
