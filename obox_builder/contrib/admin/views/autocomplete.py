"""Search and page relation choices without loading entire target tables."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.options import ModelAdmin
from obox_builder.contrib.admin.relations import related_admin, related_key
from obox_builder.contrib.admin.site import AdminSite
from obox_builder.contrib.auth.dependencies import login_required
from obox_builder.core.exceptions import PermissionDenied
from obox_builder.db.session import get_db
from obox_builder.fields import QuerySpec, QuerySpecError

PAGE_SIZE = 20


def register_autocomplete_routes(
    router: APIRouter, site: AdminSite, parent_admin: ModelAdmin
) -> None:
    async def autocomplete(
        request: Request,
        field: str,
        user: Any = Depends(login_required),
        db: AsyncSession = Depends(get_db),
    ) -> JSONResponse:
        ctx = AdminContext(request=request, user=user, db=db, site=site, admin=parent_admin)
        admin = parent_admin
        params = request.query_params
        try:
            if params.get("inline") is not None:
                index = int(params["inline"])
                inlines = parent_admin.get_inline_instances(ctx)
                if not 0 <= index < len(inlines):
                    raise LookupError
                parent = (
                    await parent_admin.get_object(ctx, params["parent"])
                    if params.get("parent")
                    else None
                )
                parent_admin.check_permission(
                    ctx, "change" if parent is not None else "add", parent
                )
                admin = inlines[index]
                ctx = replace(ctx, admin=admin, extra={"parent": parent})
                if params.get("object"):
                    ctx.obj = await admin.get_object(ctx, params["object"])
                    if parent is None or getattr(
                        ctx.obj, admin.fk_column.key
                    ) != parent_admin.get_pk(parent):
                        raise LookupError
            elif params.get("object"):
                ctx.obj = await admin.get_object(ctx, params["object"])
            admin.check_permission(ctx, "change" if ctx.obj is not None else "add", ctx.obj)
            spec = next((spec for spec in admin.get_form_fields(ctx) if spec.name == field), None)
            if (
                spec is None
                or field not in admin.autocomplete_fields
                or not spec.is_editable()
                or spec.type.name not in {"link", "multi_link"}
            ):
                raise LookupError
            if spec.editable_if is not None and not spec.editable_if(ctx.record_context()):
                raise PermissionDenied("This relation is not editable.")
            page = int(params.get("page", "1"))
            if not 1 <= page <= 1000:
                raise ValueError
        except (LookupError, ValueError, TypeError) as exc:
            raise HTTPException(400, "Invalid autocomplete request.") from exc
        target_admin, target_ctx = related_admin(ctx, spec)
        target_admin.check_permission(target_ctx, "view")
        search_specs = target_admin.get_search_specs()
        if not search_specs:
            raise HTTPException(400, "Configure search_fields on the related admin.")
        query = QuerySpec(q=params.get("q", "")[:200].strip())
        try:
            stmt = (
                query.apply(
                    target_admin.get_queryset(target_ctx),
                    target_admin.spec_map(),
                    search_specs=search_specs,
                )
                .order_by(None)
                .order_by(target_admin.pk_column())
            )
        except QuerySpecError as exc:
            raise HTTPException(400, str(exc)) from exc
        objects = list(
            (await db.scalars(stmt.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1))).unique()
        )
        key = related_key(spec)
        results = []
        for obj in objects[:PAGE_SIZE]:
            if target_admin.has_view_permission(replace(target_ctx, obj=obj), obj):
                label = str(obj)
                results.append(
                    {
                        "id": str(getattr(obj, key.key)),
                        "text": f"#{target_admin.get_pk(obj)}" if label.startswith("<") else label,
                    }
                )
        return JSONResponse(
            {"results": results, "pagination": {"more": len(objects) > PAGE_SIZE}},
            headers={"Cache-Control": "private, no-store"},
        )

    router.add_api_route(
        f"/{parent_admin.path_prefix}/autocomplete/{{field}}",
        autocomplete,
        methods=["GET"],
        name=f"{parent_admin.route_prefix}_autocomplete",
    )
