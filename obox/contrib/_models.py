"""Native model identity and permission checks shared by optional features."""

from typing import Any

import sqlalchemy as sa
from pydantic import TypeAdapter, ValidationError

from obox.apps import apps
from obox.core.exceptions import NotAuthenticated, NotFound, PermissionDenied
from obox.core.principal import has_permission
from obox.db.identity import model_label
from obox.db.query import scope_query


def installed_models() -> tuple[type[Any], ...]:
    return tuple(model for config in apps.app_configs.values() for model in config.models.values())


def permission(model: type[Any], actor: Any, action: str) -> bool:
    label, name = model_label(model)
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
    query = scope_query(queryset, model).where(column == key)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    obj = await ctx.session.scalar(query)
    if obj is None:
        raise NotFound("Record is unavailable.")
    return obj
