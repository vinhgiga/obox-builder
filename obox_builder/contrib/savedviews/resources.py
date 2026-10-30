"""Optional operations for an actor's saved queries on a model resource."""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field

from obox_builder.apps import apps
from obox_builder.exceptions import PermissionDenied
from obox_builder.fields import QuerySpec
from obox_builder.resources import (
    OperationContext,
    ReadOnlyModelResource,
    ResourceValidationError,
    operation,
)


class ViewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    config: dict[str, Any]
    id: str | None = None


class ViewID(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str


class RenameView(ViewID):
    name: str = Field(min_length=1, max_length=100)


class ViewData(BaseModel):
    id: str
    name: str
    config: dict[str, Any]


class ViewList(BaseModel):
    items: list[ViewData]


def view_data(row: Any) -> ViewData:
    return ViewData(id=str(row.id), name=row.name, config=row.config)


def view_scope(resource: Any, ctx: OperationContext) -> Any:
    from obox_builder.contrib.savedviews.models import SavedView

    if ctx.actor.id is None:
        raise PermissionDenied("An authenticated actor is required for private views.")
    return sa.select(SavedView).where(
        SavedView.owner_id == ctx.actor.id,
        SavedView.site_name == str(ctx.metadata.get("namespace", "default")),
        SavedView.model_label == resource.scope_name,
    )


async def selected(resource: Any, ctx: OperationContext, identity: str) -> Any:
    from obox_builder.contrib.savedviews.models import SavedView

    try:
        pk = int(identity)
    except ValueError as exc:
        raise LookupError("Saved view is unavailable.") from exc
    row = await ctx.session.scalar(
        view_scope(resource, ctx).where(SavedView.id == pk).with_for_update()
    )
    if row is None:
        raise LookupError("Saved view is unavailable.")
    return row


class SavedViews(ReadOnlyModelResource[Any]):
    """Add these operations before ModelResource in a resource's base classes."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not apps.apps_ready or not apps.is_installed("obox_builder.contrib.savedviews"):
            self.exclude_operations = (
                *self.exclude_operations,
                "listViews",
                "getView",
                "saveView",
                "renameView",
                "duplicateView",
                "deleteView",
            )

    @operation(name="listViews", permission="read", effect="read")
    async def list_views(self, ctx: OperationContext) -> ViewList:
        from obox_builder.contrib.savedviews.models import SavedView

        rows = await ctx.session.scalars(
            view_scope(self, ctx).order_by(SavedView.name, SavedView.id)
        )
        return ViewList(items=[view_data(row) for row in rows])

    @operation(name="getView", permission="read", effect="read")
    async def get_view(self, ctx: OperationContext, data: ViewID) -> ViewData:
        return view_data(await selected(self, ctx, data.id))

    @operation(name="saveView", permission="read")
    async def save_view(self, ctx: OperationContext, data: ViewInput) -> ViewData:
        from obox_builder.contrib.savedviews.models import SavedView

        name = data.name.strip()
        if not name:
            raise ResourceValidationError({"name": ["View name cannot be blank."]})
        if data.config.keys() - {"filter", "f", "sort", "group", "q", "size"}:
            raise ResourceValidationError({"config": ["Unknown saved query property."]})
        params = dict(data.config)
        if isinstance(params.get("filter"), dict):
            params["filter"] = json.dumps(params["filter"])
        if any(not isinstance(value, (str, list, int)) for value in params.values()):
            raise ResourceValidationError({"config": ["Invalid saved query values."]})
        config = QuerySpec.parse(params, self.query_specs(ctx)).to_params()
        config.setdefault("sort", "")
        config.setdefault("group", "")
        if ctx.actor.id is None:
            raise PermissionDenied("An authenticated actor is required for private views.")
        if data.id is None:
            row = SavedView(
                owner_id=ctx.actor.id,
                site_name=str(ctx.metadata.get("namespace", "default")),
                model_label=self.scope_name,
                name=name,
                config=config,
            )
            ctx.session.add(row)
        else:
            row = await selected(self, ctx, data.id)
            row.name, row.config = name, config
        await ctx.session.flush()
        return view_data(row)

    @operation(name="renameView", permission="read")
    async def rename_view(self, ctx: OperationContext, data: RenameView) -> ViewData:
        row = await selected(self, ctx, data.id)
        name = data.name.strip()
        if not name:
            raise ResourceValidationError({"name": ["View name cannot be blank."]})
        row.name = name
        await ctx.session.flush()
        return view_data(row)

    @operation(name="duplicateView", permission="read")
    async def duplicate_view(self, ctx: OperationContext, data: ViewID) -> ViewData:
        from obox_builder.contrib.savedviews.models import SavedView

        original = await selected(self, ctx, data.id)
        row = SavedView(
            owner_id=ctx.actor.id,
            site_name=original.site_name,
            model_label=self.scope_name,
            name=f"{original.name[:95]} copy",
            config=dict(original.config),
        )
        ctx.session.add(row)
        await ctx.session.flush()
        return view_data(row)

    @operation(name="deleteView", permission="read")
    async def delete_view(self, ctx: OperationContext, data: ViewID) -> None:
        await ctx.session.delete(await selected(self, ctx, data.id))
        await ctx.session.flush()
