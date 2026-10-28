"""Task and worker monitoring, using the durable queue as the source of truth."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, defer

from obox_builder.conf import settings
from obox_builder.contrib.admin.rendering import render
from obox_builder.contrib.admin.views.common import ensure_csrf, flash, page_context
from obox_builder.contrib.auth.dependencies import any_permission_required, permission_required
from obox_builder.contrib.auth.models import User
from obox_builder.contrib.jobs import services
from obox_builder.contrib.jobs.models import Task, TaskAttempt, Worker
from obox_builder.core.utils.time import utcnow
from obox_builder.db.session import get_db
from obox_builder.http.shortcuts import redirect

TaskReaders = Annotated[User, Depends(permission_required("jobs.view_task"))]
WorkerReaders = Annotated[User, Depends(permission_required("jobs.view_worker"))]
PAGE_SIZE = 50
STATES = {
    "PENDING": ("Queued", "yellow"),
    "STARTED": ("Running", "blue"),
    "RETRY": ("Retrying", "orange"),
    "SUCCESS": ("Succeeded", "green"),
    "FAILURE": ("Failed", "red"),
    "REVOKED": ("Revoked", "secondary"),
    "ABANDONED": ("Abandoned", "secondary"),
}


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _runtime(started: datetime | None, finished: datetime | None, now: datetime) -> str:
    if started is None:
        return "—"
    seconds = max(0, (_utc(finished or now) - _utc(started)).total_seconds())
    return f"{seconds:.2f}s" if seconds < 60 else f"{seconds / 60:.1f}m"


async def _render(request: Request, template: str, **values: Any) -> Response:
    context = await page_context(
        request, states=STATES, generated_at=utcnow(), jobs_enabled=settings.JOBS_ENABLED, **values
    )
    response = render(request, template, context)
    response.headers["Cache-Control"] = "no-store"
    return response


async def job_index(
    user: Annotated[User, Depends(any_permission_required("jobs.view_task", "jobs.view_worker"))],
) -> Response:
    target = "tasks" if user.has_perm("jobs.view_task") else "workers"
    return redirect(f"/admin/jobs/{target}", status_code=303)


async def task_list(
    request: Request,
    user: TaskReaders,
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
) -> Response:
    params = request.query_params
    filters = {key: params.get(key, "").strip()[:155] for key in ("q", "queue", "worker", "status")}
    scope: list[Any] = []
    if filters["q"]:
        scope.append(
            sa.or_(
                Task.name.icontains(filters["q"], autoescape=True),
                Task.task_id.icontains(filters["q"], autoescape=True),
            )
        )
    for key in ("queue", "worker"):
        if filters[key]:
            scope.append(getattr(Task, key) == filters[key])
    counts = dict(
        (
            await db.execute(
                sa.select(Task.status, sa.func.count()).where(*scope).group_by(Task.status)
            )
        )
        .tuples()
        .all()
    )
    if filters["status"] in STATES and filters["status"] != "ABANDONED":
        scope.append(Task.status == filters["status"])
    else:
        filters["status"] = ""
    total = counts.get(filters["status"], 0) if filters["status"] else sum(counts.values())
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    columns = {
        "created": Task.date_created,
        "name": Task.name,
        "status": Task.status,
        "eta": Task.eta,
        "retries": Task.retries,
    }
    sort = params.get("sort", "-created")
    sort = sort if sort.lstrip("-") in columns else "-created"
    column = columns[sort.lstrip("-")]
    order = column.desc() if sort.startswith("-") else column.asc()
    latest = (
        sa.select(sa.func.max(TaskAttempt.retries))
        .where(TaskAttempt.task_id == Task.task_id)
        .correlate(Task)
        .scalar_subquery()
    )
    attempt = aliased(TaskAttempt)
    rows = (
        await db.execute(
            sa.select(Task, attempt.date_started, attempt.date_done)
            .options(defer(Task.args), defer(Task.kwargs), defer(Task.result))
            .outerjoin(attempt, sa.and_(attempt.task_id == Task.task_id, attempt.retries == latest))
            .where(*scope)
            .order_by(order, Task.id.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
    ).all()
    now = utcnow()
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
    hour = sa.func.floor(sa.extract("epoch", Task.date_created) / 3600)
    volume = {
        int(bucket): count
        for bucket, count in (
            await db.execute(
                sa.select(hour, sa.func.count()).where(Task.date_created >= start).group_by(hour)
            )
        ).all()
    }
    peak = max(volume.values(), default=1) or 1
    chart = [
        {
            "hour": start + timedelta(hours=i),
            "count": volume.get(int(start.timestamp() // 3600) + i, 0),
            "height": round(100 * volume.get(int(start.timestamp() // 3600) + i, 0) / peak),
        }
        for i in range(24)
    ]
    queues = list(
        (await db.scalars(sa.select(Task.queue).distinct().order_by(Task.queue).limit(100))).all()
    )
    return await _render(
        request,
        "admin/jobs/tasks.html",
        tab="tasks",
        filters=filters,
        counts=counts,
        total=total,
        all_count=sum(counts.values()),
        page=page,
        pages=pages,
        sort=sort,
        tasks=[{"task": row, "runtime": _runtime(began, ended, now)} for row, began, ended in rows],
        chart=chart,
        queues=queues,
    )


async def task_detail(
    request: Request,
    task_id: str,
    user: TaskReaders,
    db: AsyncSession = Depends(get_db),
    before: int | None = Query(None, ge=0),
) -> Response:
    row = await db.scalar(sa.select(Task).where(Task.task_id == task_id))
    if row is None:
        raise HTTPException(404, "Task not found.")
    query = sa.select(TaskAttempt).where(TaskAttempt.task_id == task_id)
    if before is not None:
        query = query.where(TaskAttempt.retries < before)
    attempts = list(
        (await db.scalars(query.order_by(TaskAttempt.retries.desc()).limit(PAGE_SIZE + 1))).all()
    )
    older = attempts[PAGE_SIZE - 1].retries if len(attempts) > PAGE_SIZE else None
    payload = None
    if user.has_perm("jobs.view_task_payload"):
        payload = {
            key: json.dumps(getattr(row, key), indent=2, ensure_ascii=False)
            for key in ("args", "kwargs", "result")
        }
    now = utcnow()
    return await _render(
        request,
        "admin/jobs/task.html",
        tab="tasks",
        task=row,
        payload=payload,
        attempts=[
            {"attempt": item, "runtime": _runtime(item.date_started, item.date_done, now)}
            for item in attempts[:PAGE_SIZE]
        ],
        older=older,
        before=before,
        terminal=row.status in ("SUCCESS", "FAILURE", "REVOKED"),
        can_retry=user.has_perm("jobs.retry_task") and row.status in ("FAILURE", "REVOKED"),
        can_revoke=user.has_perm("jobs.revoke_task")
        and row.status in ("PENDING", "STARTED", "RETRY"),
    )


async def task_action(
    request: Request,
    task_id: str,
    action: str,
    user: TaskReaders,
    db: AsyncSession = Depends(get_db),
) -> Response:
    if action not in ("retry", "revoke"):
        raise HTTPException(404, "Unknown task action.")
    if not user.has_perm(f"jobs.{action}_task"):
        raise HTTPException(403, "You do not have permission to perform this action.")
    form = await request.form()
    await ensure_csrf(request, str(form.get("csrf_token", "")))
    if not settings.JOBS_ENABLED:
        raise HTTPException(409, "Background jobs are disabled.")
    if await db.scalar(sa.select(Task.id).where(Task.task_id == task_id)) is None:
        raise HTTPException(404, "Task not found.")
    try:
        changed = await (
            services.retry(db, task_id) if action == "retry" else services.revoke(db, task_id)
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not changed:
        raise HTTPException(409, "The task state changed. Refresh the page and try again.")
    await flash(
        request,
        "Task queued for retry."
        if action == "retry"
        else "Task revoked. A running process stops at its next lease check.",
    )
    return redirect(f"/admin/jobs/tasks/{task_id}", status_code=303)


async def worker_list(
    request: Request,
    user: WorkerReaders,
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
) -> Response:
    now = utcnow()
    cutoff = now - timedelta(seconds=settings.JOBS_WORKER_HEARTBEAT_SECONDS * 3)
    online = sa.and_(Worker.stopped_at.is_(None), Worker.heartbeat_at >= cutoff)
    total = int(await db.scalar(sa.select(sa.func.count()).select_from(Worker)) or 0)
    live = int(await db.scalar(sa.select(sa.func.count()).select_from(Worker).where(online)) or 0)
    q = request.query_params.get("q", "").strip()[:155]
    state = request.query_params.get("state", "")
    scope: list[Any] = [Worker.hostname.icontains(q, autoescape=True)] if q else []
    if state in ("online", "offline"):
        scope.append(online if state == "online" else ~online)
    filtered = int(
        await db.scalar(sa.select(sa.func.count()).select_from(Worker).where(*scope)) or 0
    )
    pages = max(1, (filtered + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    workers = list(
        (
            await db.scalars(
                sa.select(Worker)
                .where(*scope)
                .order_by(Worker.heartbeat_at.desc(), Worker.hostname)
                .offset((page - 1) * PAGE_SIZE)
                .limit(PAGE_SIZE)
            )
        ).all()
    )
    names = [worker.hostname for worker in workers]
    stats: dict[str, dict[str, int]] = {}
    for name, status, count in (
        await db.execute(
            sa.select(Task.worker, Task.status, sa.func.count())
            .where(Task.worker.in_(names))
            .group_by(Task.worker, Task.status)
        )
    ).all():
        stats.setdefault(name, {})[status] = count
    active = dict(
        (
            await db.execute(
                sa.select(Task.worker, sa.func.count())
                .where(Task.worker.in_(names), Task.status == "STARTED", Task.locked_until > now)
                .group_by(Task.worker)
            )
        )
        .tuples()
        .all()
    )
    return await _render(
        request,
        "admin/jobs/workers.html",
        tab="workers",
        page=page,
        pages=pages,
        total=total,
        online_count=live,
        offline_count=total - live,
        filtered=filtered,
        filters={"q": q, "state": state},
        workers=[
            {
                "worker": worker,
                "online": worker.stopped_at is None and _utc(worker.heartbeat_at) >= cutoff,
                "active": active.get(worker.hostname, 0),
                "stats": stats.get(worker.hostname, {}),
            }
            for worker in workers
        ],
        offline_seconds=settings.JOBS_WORKER_HEARTBEAT_SECONDS * 3,
    )
