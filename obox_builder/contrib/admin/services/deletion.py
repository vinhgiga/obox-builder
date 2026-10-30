"""HTML confirmation for the shared cascading-deletion plan."""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import replace
from typing import Any

from obox_builder.conf import settings
from obox_builder.contrib.admin.context import AdminContext, csrf_token_for
from obox_builder.resources.deletion import DeletionPlan
from obox_builder.resources.deletion import collect_deletion as resource_deletion

CONFIRM_SECONDS = 600


async def collect_deletion(
    ctx: AdminContext, objects: list[Any], *, lock: bool = False
) -> DeletionPlan:
    plan = await resource_deletion(ctx.operation_context(), objects, lock=lock)
    for effect in plan.effects.values():
        if effect.obj is None:
            continue
        view = ctx.site.get_model_admin_for(type(effect.obj))
        if view is None:
            continue
        child = replace(ctx, admin=view, obj=effect.obj)
        visible = await ctx.db.scalar(
            view.get_queryset(child)
            .with_only_columns(view.pk_column())
            .where(view.pk_column() == view.get_pk(effect.obj))
            .order_by(None)
        )
        if visible is None or not view.has_view_permission(child, effect.obj):
            effect.label = "Restricted record"
        action = {"delete": "delete", "update": "change"}.get(effect.action)
        if action and (
            visible is None or not getattr(view, f"has_{action}_permission")(child, effect.obj)
        ):
            plan.permissions.add(f"{action} {view.plural_name.lower()}")
    return plan


def confirmation_token(ctx: AdminContext, plan: DeletionPlan, stamp: int | None = None) -> str:
    stamp = int(time.time()) if stamp is None else stamp
    payload = f"{stamp}:{ctx.user.id}:{csrf_token_for(ctx.request)}:{plan.fingerprint}"
    digest = hmac.new(
        settings.SECRET_KEY.get_secret_value().encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{stamp}:{digest}"


def confirmed(ctx: AdminContext, plan: DeletionPlan, token: Any) -> bool:
    if not isinstance(token, str):
        return False
    try:
        stamp = int(token.partition(":")[0])
    except ValueError:
        return False
    return 0 <= time.time() - stamp <= CONFIRM_SECONDS and hmac.compare_digest(
        token, confirmation_token(ctx, plan, stamp)
    )
