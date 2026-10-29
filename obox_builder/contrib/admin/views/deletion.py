"""HTML confirmation page for a shared deletion plan."""

from __future__ import annotations

from typing import Any

from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.rendering import render
from obox_builder.contrib.admin.services.deletion import DeletionPlan, confirmation_token
from obox_builder.contrib.admin.views.common import page_context


async def deletion_page(
    ctx: AdminContext,
    plan: DeletionPlan,
    *,
    bulk: bool = False,
    error: str = "",
    status_code: int = 200,
) -> Any:
    admin = ctx.admin
    if admin is None:
        raise RuntimeError("Deletion requires a model admin.")
    context = await page_context(
        ctx.request,
        admin=admin,
        plan=plan,
        bulk=bulk,
        error=error,
        confirmation=confirmation_token(ctx, plan),
        selected=[str(admin.get_pk(obj)) for obj in plan.roots],
        action=f"{admin.list_path()}/actions"
        if bulk
        else admin.delete_path(admin.get_pk(plan.roots[0])),
    )
    return render(ctx.request, "admin/model/delete.html", context, status_code=status_code)
