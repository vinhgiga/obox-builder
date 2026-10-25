"""Discover only explicitly exposed models and safe request-specific fields."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from obox_builder.contrib.admin.api.deps import DB, USER
from obox_builder.contrib.admin.api.problems import Problem
from obox_builder.contrib.admin.api.serializers import api_specs
from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.services import RecordService
from obox_builder.fields.types.choice import normalize_choices


def model_context(request: Request, user: Any, db: Any, label: str) -> AdminContext:
    site = request.state.admin_site
    admin = site.get_registry().get(label)
    token = getattr(request.state, "api_token", None)
    extra = {"api": True, **({"api_scopes": token.scopes} if token is not None else {})}
    ctx = AdminContext(request, user, db, site, admin, extra=extra)
    if admin is None or not admin.api_enabled or not RecordService(ctx).allowed("view"):
        raise Problem(404, "not_found", "Model is unavailable.")
    return ctx


def register_meta(router: APIRouter) -> None:
    @router.get("/meta")
    async def meta(request: Request, user: USER, db: DB) -> dict[str, Any]:
        site = request.state.admin_site
        models = []
        for admin in site.get_registry().values():
            try:
                model_ctx = model_context(request, user, db, admin.model_label)
            except Problem:
                continue
            if admin.api_enabled:
                models.append(
                    {
                        "label": admin.model_label,
                        "name": admin.name,
                        "plural_name": admin.plural_name,
                        "can_add": RecordService(model_ctx).allowed("add"),
                    }
                )
        return {
            "site_header": site.site_header,
            "site_title": site.site_title,
            "models": models,
            "navigation": [
                {
                    "label": "Models",
                    "items": [
                        {"label": model["plural_name"], "model": model["label"]} for model in models
                    ],
                }
            ],
        }

    @router.get("/models/{label}/schema")
    async def schema(request: Request, label: str, user: USER, db: DB) -> dict[str, Any]:
        from obox_builder.contrib.admin.api.actions import api_actions

        ctx = model_context(request, user, db, label)
        admin = ctx.admin
        assert admin is not None  # noqa: S101
        specs = api_specs(ctx)
        return {
            "label": admin.model_label,
            "name": admin.name,
            "plural_name": admin.plural_name,
            "fields": [
                {
                    "name": spec.name,
                    "label": spec.label,
                    "type": spec.type.name,
                    "nullable": spec.nullable,
                    "required": spec.required,
                    "readonly": not spec.is_editable(),
                    "help_text": spec.help_text,
                    "choices": [
                        {"value": value, "label": label}
                        for value, label in normalize_choices(spec.options.get("choices"))
                    ],
                    "operators": [
                        {"name": op.name, "label": op.label, "arity": op.arity}
                        for op in spec.type.operators.values()
                    ],
                    "dynamic": [
                        name
                        for name in ("show_if", "editable_if", "required_if", "valid_if", "initial")
                        if callable(getattr(spec, name))
                    ],
                }
                for spec in specs
            ],
            "layout": [
                {**group, "rows": [[spec.name for spec in row] for row in group["rows"]]}
                for group in admin.get_form_layout(specs, ctx)
            ],
            "list_display": [spec.name for spec in admin.get_list_specs(ctx) if not spec.secret],
            "ordering": list(admin.get_ordering(ctx)),
            "can_add": RecordService(ctx).allowed("add"),
            "actions": [
                {"name": spec.name, "description": spec.description} for spec in api_actions(ctx)
            ],
        }
