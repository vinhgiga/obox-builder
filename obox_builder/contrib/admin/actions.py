"""Bulk actions for list pages (Django-style ``actions``).

An action is an async callable ``(admin, ctx, queryset)`` that either returns
``None`` (the caller then redirects back to the filtered list) or a response
(rendered as-is, e.g. a confirmation page).

Actions are declared on a ModelView::

    class PostAdmin(ModelView):
        actions = ("mark_published", delete_selected)

        @action(description="Publish selected posts", permissions=("change",))
        async def mark_published(self, ctx, queryset): ...
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from fastapi.responses import Response

from obox_builder.http.messages import add_message

if TYPE_CHECKING:
    from obox_builder.contrib.admin.context import AdminContext
    from obox_builder.contrib.admin.options import ModelView

__all__ = ["delete_selected"]


async def delete_selected(
    admin: ModelView, ctx: AdminContext, queryset: sa.Select[Any]
) -> Response | None:
    """Delete every row matched by ``queryset`` (built-in, all-or-nothing).

    Rows are deleted through the ORM one at a time so per-row side effects
    (row history, signals) observe each change individually.
    """
    from obox_builder.contrib.admin.services.deletion import collect_deletion, confirmed
    from obox_builder.contrib.admin.views.deletion import deletion_page

    objects = list((await ctx.db.scalars(queryset)).unique())
    pks = [admin.get_pk(obj) for obj in objects]
    if not pks:
        await add_message(ctx.request, "No matching records to delete.", "warning")
        return None
    plan = await collect_deletion(ctx, objects, lock=True)
    if not plan.allowed:
        return await deletion_page(
            ctx, plan, bulk=True, status_code=403 if plan.permissions else 409
        )
    data = await ctx.request.form()
    token = data.get("_delete_confirmation")
    if not confirmed(ctx, plan, token):
        return await deletion_page(
            ctx,
            plan,
            bulk=True,
            error="Related records changed or confirmation expired. Review the deletion again."
            if token
            else "",
            status_code=409 if token else 200,
        )
    async with ctx.db.begin_nested():
        await admin.resource.execute("deleteSelected", admin.operation_context(ctx), target=objects)
        await ctx.db.flush()
    noun = admin.name.lower() if len(pks) == 1 else admin.plural_name.lower()
    await add_message(ctx.request, f"Successfully deleted {len(pks)} {noun}.", "success")
    return None
