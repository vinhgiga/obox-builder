"""Authenticated model contexts for explicitly registered custom API routes."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from obox_builder.contrib.admin.api.deps import DB, USER
from obox_builder.contrib.admin.api.meta import model_context
from obox_builder.contrib.admin.services import RecordService
from obox_builder.contrib.auth.tokens import scope_allows
from obox_builder.core.exceptions import PermissionDenied


def api_route_context(admin: Any, permission: str | None) -> Any:
    async def dependency(request: Request, user: USER, db: DB) -> Any:
        ctx = model_context(request, user, db, admin.model_label)
        if permission is not None:
            if permission in {"view", "add", "change", "delete"}:
                RecordService(ctx).check_permission(permission)
            else:
                admin.check_permission(ctx, permission)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            operation = "delete" if request.method == "DELETE" else "write"
            scopes = ctx.extra.get("api_scopes")
            if scopes is not None and not scope_allows(scopes, admin.model_label, operation):
                raise PermissionDenied("Token cannot modify this resource.")
            if permission in {None, "view"}:
                RecordService(ctx).check_permission("delete" if operation == "delete" else "change")
        request.state.admin_context = ctx
        return ctx

    return dependency
