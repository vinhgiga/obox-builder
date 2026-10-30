"""Job monitoring and task controls share one contract across transports."""

from __future__ import annotations

from pydantic import BaseModel

from obox_builder.contrib.jobs import services
from obox_builder.contrib.jobs.models import Task, Worker
from obox_builder.resources import (
    OperationContext,
    ReadOnlyModelResource,
    ResourceConflict,
    ResourcePolicy,
    capability,
    operation,
    register,
)


class TaskResult(BaseModel):
    id: str
    changed: bool


class TaskPolicy(ResourcePolicy):
    def allows(self, ctx, operation, obj=None):
        return ctx.actor.has_permission("jobs.task.read")


@register()
class TaskResource(ReadOnlyModelResource[Task]):
    name = "jobs.task"
    model = Task
    fields = (
        "id",
        "name",
        "queue",
        "args",
        "kwargs",
        "status",
        "result",
        "traceback",
        "worker",
        "retries",
        "max_retries",
        "eta",
        "expires",
        "priority",
        "date_created",
        "date_done",
    )
    read_payload = capability("Can read task payloads")
    fields_config = dict.fromkeys(
        ("args", "kwargs", "result", "traceback"), {"read_permission": read_payload}
    )
    ordering = ("-date_created",)
    search_fields = ("name",)
    policy = TaskPolicy()

    @operation(scope="record", label="Can retry tasks")
    async def retry(self, ctx: OperationContext, obj: Task) -> TaskResult:
        changed = await services.retry(ctx.session, obj.id)
        if not changed:
            raise ResourceConflict("The task state changed. Refresh and try again.")
        return TaskResult(id=obj.id, changed=changed)

    @operation(scope="record", label="Can revoke tasks")
    async def revoke(self, ctx: OperationContext, obj: Task) -> TaskResult:
        changed = await services.revoke(ctx.session, obj.id)
        if not changed:
            raise ResourceConflict("The task state changed. Refresh and try again.")
        return TaskResult(id=obj.id, changed=changed)


@register()
class WorkerResource(ReadOnlyModelResource[Worker]):
    name = "jobs.worker"
    model = Worker
    fields = (
        "hostname",
        "pid",
        "queues",
        "concurrency",
        "started_at",
        "heartbeat_at",
        "stopped_at",
    )
