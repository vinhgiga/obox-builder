"""Native model identity and permission checks shared by optional features."""

from typing import Any

import sqlalchemy as sa
from pydantic import TypeAdapter, ValidationError

from obox._auth import has_permission
from obox._exceptions import NotAuthenticated, NotFound, PermissionDenied
from obox.apps import apps
from obox.db import _scope
from obox.views.generic.detail import _model_label


def installed_models() -> tuple[type[Any], ...]:
    return tuple(model for config in apps.app_configs.values() for model in config.models.values())


def permission(model: type[Any], actor: Any, action: str) -> bool:
    label, name = _model_label(model)
    action = {"list": "view", "retrieve": "view", "create": "add", "update": "change"}.get(
        action, action
    )
    return has_permission(actor, f"{label}.{action}_{name}")


def authorize(model: type[Any], actor: Any, action: str) -> None:
    if not getattr(actor, "is_authenticated", False) or not getattr(actor, "is_active", False):
        raise NotAuthenticated("Authentication is required.")
    if not permission(model, actor, action):
        raise PermissionDenied("This operation is not permitted.")


async def get_object(
    model: type[Any], identity: Any, ctx: Any, queryset: Any, *, lock: bool = False
) -> Any:
    mapper = sa.inspect(model)
    column = mapper.primary_key[0]
    try:
        key = TypeAdapter(column.type.python_type).validate_python(identity)
    except (TypeError, ValueError, ValidationError) as exc:
        raise NotFound("Record is unavailable.") from exc
    query = _scope(queryset, model).where(column == key)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    obj = await ctx.session.scalar(query)
    if obj is None:
        raise NotFound("Record is unavailable.")
    return obj
