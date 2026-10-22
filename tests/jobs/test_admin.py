from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import obox_builder
import pytest
import sqlalchemy as sa
from obox_builder.conf import SETTINGS_MODULE_ENV, settings
from obox_builder.contrib.jobs.models import Task, TaskAttempt, Worker
from obox_builder.core.utils.time import utcnow
from obox_builder.db.session import get_db, session_scope
from sqlalchemy.ext.asyncio import AsyncSession

from tests.admin.conftest import (
    build_app,
    csrf_for,
    ensure_permissions,
    grant_permissions,
    login_session,
    make_user,
)
from tests.jobs import read


@pytest.fixture
def jobs_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from obox_builder.contrib.jobs import registry

    monkeypatch.setattr(registry, "_tasks", {})
    monkeypatch.setenv(SETTINGS_MODULE_ENV, "tests.support.jobs_admin_settings")
    obox_builder.setup()
    if settings.DATABASE_URL.startswith("sqlite"):
        monkeypatch.setattr(
            settings.load(), "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'admin.db'}"
        )


@pytest.fixture
async def client(db: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    await ensure_permissions()
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", headers={"accept": "text/html"}
    ) as http:
        yield http


async def sign_in(client: httpx.AsyncClient, *permissions: str) -> str:
    await make_user("operator")
    await grant_permissions("operator", *permissions)
    token = await login_session(client, "operator")
    return await csrf_for("operator", token)


async def make_task(status: str = "PENDING", **values: Any) -> str:
    task_id = str(uuid.uuid4())
    defaults: dict[str, Any] = {
        "task_id": task_id,
        "name": "jobs.cpu",
        "status": status,
        "args": ["private-payload"],
        "kwargs": {},
        "result": {"key": "private-result"},
    }
    if status == "STARTED":
        defaults.update(lock_token=uuid.uuid4(), locked_until=utcnow() + timedelta(minutes=1))
    async with session_scope() as db:
        db.add(Task(**{**defaults, **values}))
    return task_id


@pytest.mark.parametrize(
    "url", ["/admin/jobs", "/admin/jobs/tasks", "/admin/jobs/workers", "/admin/jobs/tasks/missing"]
)
async def test_monitor_requires_authentication(client: httpx.AsyncClient, url: str) -> None:
    response = await client.get(url)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")


async def test_permissions_and_navigation_are_separate(client: httpx.AsyncClient) -> None:
    await sign_in(client, "jobs.view_worker")
    assert (await client.get("/admin/jobs/tasks")).status_code == 403
    workers = await client.get("/admin/jobs/workers")
    assert workers.status_code == 200
    assert 'href="/admin/jobs/workers"' in workers.text
    assert 'href="/admin/jobs/tasks"' not in workers.text
    assert (await client.get("/admin/jobs")).headers["location"] == "/admin/jobs/workers"


async def test_task_filters_search_sort_and_pagination(client: httpx.AsyncClient) -> None:
    await sign_in(client, "jobs.view_task")
    target = await make_task("FAILURE", name="jobs.special", queue="cpu", worker="worker-a")
    other = await make_task(name="jobs.other", queue="emails")
    response = await client.get(
        "/admin/jobs/tasks",
        params={
            "q": "special",
            "status": "FAILURE",
            "queue": "cpu",
            "worker": "worker-a",
            "sort": "name",
        },
    )
    assert response.status_code == 200
    assert target in response.text
    assert other not in response.text
    assert "private-payload" not in response.text
    assert "private-result" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert (await client.get("/admin/jobs/tasks?q=%25")).status_code == 200
    assert target not in (await client.get("/admin/jobs/tasks?q=%25")).text
    async with session_scope() as db:
        db.add_all([Task(task_id=f"page-task-{i}", name="jobs.cpu") for i in range(51)])
    page = await client.get("/admin/jobs/tasks?page=2")
    assert "Page 2 of 2" in page.text
    assert "page-task-50" not in page.text
    assert (await client.get("/admin/jobs/tasks?page=-1")).status_code == 422


@pytest.mark.parametrize("can_payload", [False, True])
async def test_detail_payload_permissions_attempts_and_html_escaping(
    client: httpx.AsyncClient, can_payload: bool
) -> None:
    permissions = ["jobs.view_task", *(["jobs.view_task_payload"] if can_payload else [])]
    await sign_in(client, *permissions)
    task_id = await make_task(
        "FAILURE", name="<script>alert(1)</script>", traceback="<b>failure</b>"
    )
    async with session_scope() as db:
        db.add(
            TaskAttempt(
                task_id=task_id,
                retries=0,
                worker="worker-a",
                status="FAILURE",
                traceback="safe-error",
                date_done=utcnow(),
            )
        )
    response = await client.get(f"/admin/jobs/tasks/{task_id}")
    assert response.status_code == 200
    assert "Execution history" in response.text
    assert "safe-error" in response.text
    assert "&lt;script&gt;" in response.text
    assert "<script>alert(1)</script>" not in response.text
    assert ("private-payload" in response.text) == can_payload
    assert ("private-result" in response.text) == can_payload
    assert "Retry task" not in response.text


@pytest.mark.parametrize(
    "action,status,expected", [("retry", "FAILURE", "RETRY"), ("revoke", "PENDING", "REVOKED")]
)
async def test_actions_require_permission_and_csrf_and_reject_get(
    client: httpx.AsyncClient, action: str, status: str, expected: str
) -> None:
    csrf = await sign_in(client, "jobs.view_task")
    task_id = await make_task(status)
    url = f"/admin/jobs/tasks/{task_id}/{action}"
    assert (await client.post(url, data={"csrf_token": csrf})).status_code == 403
    await grant_permissions("operator", f"jobs.{action}_task")
    assert (await client.get(url)).status_code == 405
    assert (await client.post(url)).status_code == 403
    assert (await client.post(url, data={"csrf_token": "wrong"})).status_code == 403
    response = await client.post(url, data={"csrf_token": csrf})
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/jobs/tasks/{task_id}"
    assert (await read(sa.select(Task).where(Task.task_id == task_id))).status == expected


async def test_missing_and_terminal_task_actions(client: httpx.AsyncClient) -> None:
    csrf = await sign_in(client, "jobs.view_task", "jobs.retry_task")
    task_id = await make_task("SUCCESS")
    assert (await client.get("/admin/jobs/tasks/missing")).status_code == 404
    assert (
        await client.post("/admin/jobs/tasks/missing/retry", data={"csrf_token": csrf})
    ).status_code == 404
    assert (
        await client.post(f"/admin/jobs/tasks/{task_id}/retry", data={"csrf_token": csrf})
    ).status_code == 409


async def test_workers_include_idle_stale_and_stopped_processes(client: httpx.AsyncClient) -> None:
    await sign_in(client, "jobs.view_worker", "jobs.view_task")
    now = utcnow()
    async with session_scope() as db:
        for name, heartbeat, stopped in [
            ("idle-online", now, None),
            ("crashed-offline", now - timedelta(minutes=1), None),
            ("stopped-offline", now, now),
        ]:
            db.add(
                Worker(
                    hostname=name,
                    pid=123,
                    queues=["cpu"],
                    concurrency=4,
                    started_at=now - timedelta(hours=1),
                    heartbeat_at=heartbeat,
                    stopped_at=stopped,
                )
            )
    await make_task("STARTED", worker="idle-online")
    online = await client.get("/admin/jobs/workers?state=online")
    assert online.status_code == 200
    assert "idle-online" in online.text
    assert "crashed-offline" not in online.text
    assert "stopped-offline" not in online.text
    offline = await client.get("/admin/jobs/workers?state=offline")
    assert "idle-online" not in offline.text
    assert "crashed-offline" in offline.text
    assert "stopped-offline" in offline.text
    assert "Active / Slots" in online.text


async def test_attempt_history_uses_nonoverlapping_pages(client: httpx.AsyncClient) -> None:
    await sign_in(client, "jobs.view_task")
    task_id = await make_task("FAILURE", retries=50, max_retries=50)
    async with session_scope() as db:
        db.add_all(
            TaskAttempt(
                task_id=task_id, retries=i, worker=f"attempt-worker-{i:02}", status="FAILURE"
            )
            for i in range(51)
        )
    latest = await client.get(f"/admin/jobs/tasks/{task_id}")
    assert "attempt-worker-50" in latest.text
    assert "attempt-worker-00" not in latest.text
    assert 'href="?before=1"' in latest.text
    older = await client.get(f"/admin/jobs/tasks/{task_id}?before=1")
    assert "attempt-worker-00" in older.text
    assert "attempt-worker-01" not in older.text
    assert "Older attempts" not in older.text


async def test_disabled_jobs_preserve_history_but_reject_actions(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    csrf = await sign_in(client, "jobs.view_task", "jobs.retry_task")
    task_id = await make_task("FAILURE")
    monkeypatch.setattr(settings.load(), "JOBS_ENABLED", False)
    response = await client.get(f"/admin/jobs/tasks/{task_id}")
    assert response.status_code == 200
    assert "Background jobs are disabled" in response.text
    assert "Retry task" not in response.text
    assert (
        await client.post(f"/admin/jobs/tasks/{task_id}/retry", data={"csrf_token": csrf})
    ).status_code == 409
    assert (await read(sa.select(Task).where(Task.task_id == task_id))).status == "FAILURE"


async def test_sqlite_request_transactions_serialize_read_then_write(db: AsyncSession) -> None:
    if db.get_bind().dialect.name != "sqlite":
        pytest.skip("SQLite read-to-write lock upgrade regression")
    task_id = await make_task(max_retries=20)

    async def increment() -> None:
        async with asynccontextmanager(get_db)() as request_db:
            row = await request_db.scalar(sa.select(Task).where(Task.task_id == task_id))
            assert row is not None
            await asyncio.sleep(0)  # Allow competing requests between the read and write.
            row.retries += 1

    await asyncio.gather(*(increment() for _ in range(10)))
    assert (await read(sa.select(Task).where(Task.task_id == task_id))).retries == 10
