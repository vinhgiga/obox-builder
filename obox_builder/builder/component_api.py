"""JSON adapters for independently published components."""

from __future__ import annotations

from types import GenericAlias
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Depends
from pydantic import create_model

from obox_builder.builder.http import ApiRoute
from obox_builder.components import (
    ComponentDescription,
    ComponentDescriptions,
    ComponentRecords,
    RecordResults,
)
from obox_builder.context import Context
from obox_builder.errors import NotAuthenticated

if TYPE_CHECKING:
    from obox_builder.application import Obox
    from obox_builder.builder.components import ComponentService


class ComponentRoute(ApiRoute):
    cache_control = "private, no-store"


def build_component_api(obox: Obox, app_label: str) -> APIRouter:
    router = APIRouter(route_class=ComponentRoute)
    path = f"{obox.settings.api_prefix}/{app_label}/components"
    dependency = Depends(obox.context_dependency("api"), scope="function")
    services = [service for (label, _), service in obox.components.items() if label == app_label]

    async def describe_all(ctx: Context = dependency) -> Any:
        if not getattr(ctx.principal, "is_authenticated", False) or not getattr(
            ctx.principal, "is_active", False
        ):
            raise NotAuthenticated("Authentication is required.")
        values = [
            await service.describe(ctx=ctx) for service in services if await service.permitted(ctx)
        ]
        return {"components": values}

    router.add_api_route(
        path + ":describe",
        describe_all,
        methods=["GET"],
        response_model=ComponentDescriptions,
        name=f"obox_components_{app_label}",
    )

    def add(service: ComponentService) -> None:
        prefix = f"{path}/{service.name}"
        identity = service.identity

        async def describe(ctx: Context = dependency) -> Any:
            return await service.describe(ctx=ctx)

        router.add_api_route(
            prefix + ":describe",
            describe,
            methods=["GET"],
            response_model=ComponentDescription,
            name=f"obox_component_{identity}_describe",
        )
        if service.query_model is None:
            return

        async def query(data: Any, ctx: Context = dependency) -> Any:
            return await service.query(data, ctx=ctx)

        query.__annotations__["data"] = service.query_model
        router.add_api_route(
            prefix + ":query",
            query,
            methods=["POST"],
            response_model=service.result_model,
            name=f"obox_component_{identity}_query",
        )
        if not getattr(service.definition, "drill", False):
            return

        async def records(data: ComponentRecords, ctx: Context = dependency) -> Any:
            return await service.records(data, ctx=ctx)

        record_model = RecordResults
        if service.resource is not None:
            builder = service.resource.builder
            item_model = builder.api.get_read_schema(builder=builder)
            record_model = create_model(
                f"{app_label}_{service.name}_ComponentRecords",
                __base__=RecordResults,
                records=(cast(Any, GenericAlias(list, item_model)), ...),
            )
        router.add_api_route(
            prefix + ":records",
            records,
            methods=["POST"],
            response_model=record_model,
            response_model_exclude_unset=True,
            name=f"obox_component_{identity}_records",
        )

    for service in services:
        add(service)
    return router
