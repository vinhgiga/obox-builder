"""HTML view configuration and overridable data and rendering methods."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from obox._context import Context
from obox.contrib.resources.surface import _Presentation
from obox.db._fields import FieldRefs


class ViewOptions(_Presentation):
    enabled = True
    list_template = "obox/list.html"
    detail_template = "obox/detail.html"
    list_display: FieldRefs | None = None
    detail_fields: FieldRefs | None = None
    related: FieldRefs = ()
    operations: tuple[str, ...] = ("create", "update", "delete")
    page_size: int | None = None
    inline_edit: bool | None = None

    def get_list_display(self, ctx: Context) -> Sequence[str]:
        return cast(Sequence[str], self.builder.list_display(ctx))

    def get_list_filter(self, ctx: Context) -> Sequence[str]:
        allowed = self.builder.readable(ctx)
        return tuple(name for name in self.builder.filter_fields if name in allowed)

    def get_operations(self, ctx: Context, *, instance: Any = None) -> Sequence[str]:
        return self.operations

    def get_context(self, ctx: Context, *, instance: Any = None) -> Mapping[str, Any]:
        return {}

    async def get_queryset(self, ctx: Context, **options: Any) -> Sequence[Any]:
        return cast(Sequence[Any], await self.service.list(ctx=ctx, **options))

    async def get_object(self, id: Any, ctx: Context) -> Any:
        return await self.service.get(id, ctx=ctx)

    async def serialize(
        self, instance: Any, ctx: Context, *, fields: Sequence[str] | None = None
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self.service.serialize(
                instance, ctx=ctx, fields=self.builder.view_details if fields is None else fields
            ),
        )

    async def delete(self, id: Any, ctx: Context) -> None:
        await self.service.delete(id, ctx=ctx)

    def render(
        self,
        template: str,
        request: Request,
        ctx: Context,
        *,
        context: Mapping[str, Any],
        status: int = 200,
    ) -> HTMLResponse:
        return cast(
            HTMLResponse,
            self.builder.app.templates.TemplateResponse(
                request=request,
                name=template,
                context=dict(context),
                status_code=status,
                headers={"Cache-Control": "private, no-store"},
            ),
        )

    def build_router(self) -> APIRouter:
        from obox.contrib.resources.ui import build_ui

        return build_ui(self.builder.app, self.builder, self.service)
