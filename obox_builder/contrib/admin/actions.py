"""Bulk actions for list pages (Django-style ``actions``).

An action is an async callable ``(admin, ctx, queryset)`` that either returns
``None`` (the caller then redirects back to the filtered list) or a response
(rendered as-is, e.g. a confirmation page).

Actions are declared on a ModelAdmin::

    class PostAdmin(ModelAdmin):
        actions = ("mark_published", delete_selected)

        @action(description="Publish selected posts", permissions=("change",))
        async def mark_published(self, ctx, queryset): ...
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError

from obox_builder.http.messages import add_message

if TYPE_CHECKING:
    from obox_builder.contrib.admin.context import AdminContext
    from obox_builder.contrib.admin.options import ModelAdmin

__all__ = ["action", "delete_selected"]


def action(
    func: Callable[..., Any] | None = None,
    *,
    description: str = "",
    permissions: Sequence[str] = (),
) -> Any:
    """Mark a method/function as a bulk action.

    ``permissions`` lists admin perm codenames (``add``/``change``/``delete``);
    the action is offered only when the user holds at least one of them.
    """

    def decorate(target: Callable[..., Any]) -> Callable[..., Any]:
        target.description = description  # type: ignore[attr-defined]
        target.allowed_permissions = tuple(permissions)  # type: ignore[attr-defined]
        return target

    if func is not None:
        return decorate(func)
    return decorate


@action(description="Delete selected %(verbose_name_plural)s", permissions=("delete",))
async def delete_selected(
    admin: ModelAdmin, ctx: AdminContext, queryset: sa.Select[Any]
) -> Response | None:
    """Delete every row matched by ``queryset`` (built-in, all-or-nothing).

    Rows are deleted through the ORM one at a time so per-row side effects
    (row history, signals) observe each change individually.
    """
    from obox_builder.contrib.admin.deletion import collect_deletion, confirmed, deletion_page

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
    try:
        async with ctx.db.begin_nested():
            await admin.delete_queryset(ctx, queryset)
            await ctx.db.flush()
    except IntegrityError as exc:
        await add_message(
            ctx.request,
            f"Could not delete: the database rejected the change ({exc.orig}).",
            "error",
        )
        return None
    noun = admin.name.lower() if len(pks) == 1 else admin.plural_name.lower()
    await add_message(ctx.request, f"Successfully deleted {len(pks)} {noun}.", "success")
    return None
