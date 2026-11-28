"""Typed model routes for native FastAPI applications."""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from copy import copy
from types import GenericAlias, MappingProxyType
from typing import Annotated, Any, Generic, Literal, TypeVar, cast
from urllib.parse import quote

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.routing import RouteContext, iter_route_contexts
from pydantic import BaseModel, ConfigDict, Field, SkipValidation, create_model
from pydantic.alias_generators import to_camel

from obox.components.query import parameters
from obox.core.exceptions import DomainError, ImproperlyConfigured, InvalidQuery
from obox.core.principal import valid_permission
from obox.db.fields import schema
from obox.db.filters import MAX_FILTER_LENGTH
from obox.db.identity import model_label
from obox.resources import ModelResource
from obox.runtime.context import Context as Context
from obox.web.dependencies import api_context as api_context
from obox.web.dependencies import api_principal_context
from obox.web.routes import ApiRoute
from obox.web.schemas import ApiRequest as ApiRequest
from obox.web.schemas import ApiResponse as ApiResponse

ModelT = TypeVar("ModelT")
RouterMethod = Literal["list", "retrieve", "create", "patch", "delete"]

_ROUTES: dict[RouterMethod, tuple[str, str, int, str]] = {
    "list": ("GET", "", 200, "view"),
    "retrieve": ("GET", "/{id}", 200, "view"),
    "create": ("POST", "", 201, "add"),
    "patch": ("PATCH", "/{id}", 200, "change"),
    "delete": ("DELETE", "/{id}", 204, "delete"),
}

__all__ = ["ApiRequest", "ApiResponse", "Context", "ModelApi", "RouterMethod", "api_context"]


class _ModelRoute(ApiRoute):
    cache_control = "private, no-store"


class _CollectionParams(ApiRequest):
    model_config = ConfigDict(extra="ignore")
    q: str = ""
    filter: str = Field(default="", max_length=MAX_FILTER_LENGTH)
    limit: int | None = Field(default=None, ge=1)
    offset: int = Field(default=0, ge=0, le=2**31 - 1)
    include_total: bool = False


class _ListParams(_CollectionParams):
    ordering: str = ""


class _GroupParams(_CollectionParams):
    group: str = Field(description="One to three group fields.")


class _Page(ApiResponse):
    limit: int
    offset: int
    has_more: bool
    total: int | None


class _Group(ApiResponse):
    key: dict[str, Any]
    count: int


class _Groups(_Page):
    by: list[str]
    total: int | None = None
    items: list[_Group]


def _body_schema(resource: ModelResource[Any], action: RouterMethod) -> type[ApiRequest]:
    fields: dict[str, Any] = {}
    for name, info in resource.input_schema.model_fields.items():
        annotation = (
            Annotated[info.annotation, *info.metadata] if info.metadata else info.annotation
        )
        fields[name] = (
            Annotated[annotation, SkipValidation],
            ... if action == "create" and info.is_required() else None,
        )
    return create_model(f"{resource.model.__name__}{action.title()}", __base__=ApiRequest, **fields)


def check_route_conflicts(routes: Iterable[RouteContext], *, all_routes: bool = False) -> None:
    registered: dict[tuple[str, str], bool] = {}
    for route in routes:
        if route.path is None:
            continue
        path = re.sub(r"\{[^}]+\}", "{}", route.path)
        generated = isinstance(route.original_route, _ModelRoute)
        for method in route.methods or ():
            if (method, path) in registered and (
                all_routes or generated or registered[method, path]
            ):
                raise ImproperlyConfigured(f"Duplicate API route: {method} {route.path}.")
            registered[method, path] = generated


class ModelApi(Generic[ModelT]):
    """Generate protected CRUD routes from a ModelResource."""

    model: type[ModelT]
    prefix: str
    slug: str | None = None
    resource: type[ModelResource[ModelT]]
    _resource: ModelResource[ModelT]
    actions: Collection[RouterMethod] | None = None
    permissions: Mapping[RouterMethod, str] = MappingProxyType({})
    dependencies: Sequence[Any] = ()
    principal_dependency: Callable[..., Any] | None = None
    default_limit: int = 50
    max_limit: int = 100
    tags: Sequence[str] | None = None
    name_prefix: str | None = None
    error_status: Mapping[type[DomainError], int] = MappingProxyType({})

    @classmethod
    def as_router(cls, prefix: str | None = None) -> APIRouter:
        return cls().build_router(prefix)

    def get_resource(self, ctx: Context) -> ModelResource[ModelT]:
        resources = ctx.session.info.setdefault("obox.resources", {})
        key = (id(self._resource), id(ctx.principal))
        if key not in resources:
            resources[key] = self._resource.bind(ctx.session, ctx.principal)
        return cast(ModelResource[ModelT], resources[key])

    def build_router(self, prefix: str | None = None) -> APIRouter:
        if not isinstance(getattr(self, "resource", None), type) or not issubclass(
            self.resource, ModelResource
        ):
            raise ImproperlyConfigured("Define a ModelResource class.")
        return copy(self).build_resource_router(
            prefix if prefix is not None else self.prefix, self.resource()
        )

    def build_resource_router(self, prefix: str, resource: ModelResource[ModelT]) -> APIRouter:
        if not re.fullmatch(r"/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", prefix):
            raise ImproperlyConfigured("Provide an absolute API prefix.")
        actions = tuple(_ROUTES) if self.actions is None else self.actions
        if (
            isinstance(actions, str)
            or not isinstance(actions, Collection)
            or any(not isinstance(action, str) or action not in _ROUTES for action in actions)
            or len(actions) != len(set(actions))
        ):
            raise ImproperlyConfigured("Use unique, supported ModelApi action names.")
        if not isinstance(self.permissions, Mapping) or any(
            action not in _ROUTES
            or not isinstance(permission, str)
            or not valid_permission(permission)
            for action, permission in self.permissions.items()
        ):
            raise ImproperlyConfigured("Use supported ModelApi permission method names and values.")
        if (
            type(self.default_limit) is not int
            or type(self.max_limit) is not int
            or not 1 <= self.default_limit <= self.max_limit < 2**31 - 1
        ):
            raise ImproperlyConfigured("Choose positive API page limits in ascending order.")
        if not isinstance(self.error_status, Mapping) or any(
            not isinstance(error, type)
            or not issubclass(error, DomainError)
            or type(status) is not int
            or not 400 <= status <= 599
            for error, status in self.error_status.items()
        ):
            raise ImproperlyConfigured("Map domain errors to HTTP error statuses.")
        route_class = (
            type("ModelRoute", (_ModelRoute,), {"error_status_overrides": dict(self.error_status)})
            if self.error_status
            else _ModelRoute
        )
        self._resource, self.model = resource, resource.model
        if "create" in actions:
            resource.check_create(resource.writable)
        read = schema(
            f"{self.model.__name__}Read", resource.specs, resource.readable, base=ApiResponse
        )
        router = APIRouter(
            prefix=prefix,
            tags=list(self.tags) if self.tags is not None else [prefix.rsplit("/", 1)[-1]],
            dependencies=list(self.dependencies),
        )
        resolver = type(self).principal_dependency
        if resolver is not None and not callable(resolver):
            raise ImproperlyConfigured("Choose a callable principal dependency.")
        context = api_principal_context(resolver) if resolver is not None else None

        def require(permission: str) -> Any:
            policy = api_context(permission=permission, csrf=True)
            return Depends(policy.with_context(context) if context is not None else policy)

        label, model_name = model_label(self.model)
        for action, (verb, path, status, operation) in _ROUTES.items():
            if action not in actions:
                continue
            dependency = require(f"{label}.{operation}_{model_name}")
            additional = self.permissions.get(action)
            extra = [require(additional)] if additional else None
            name = (
                f"{self.name_prefix}-{action}"
                if self.name_prefix
                else f"obox:api:{prefix}:{action}"
            )
            read_model: Any = None if action == "delete" else read
            if action == "list":
                items = cast("type[Any]", GenericAlias(list, read))
                read_model = create_model(
                    f"{self.model.__name__}Page", __base__=_Page, items=(items, ...)
                )
            endpoint = self._endpoint(action, dependency, read, "retrieve" in actions)
            for index, route_path in enumerate((path,) if path else ("", "/")):
                router.add_api_route(
                    route_path,
                    endpoint,
                    methods=[verb],
                    status_code=status,
                    response_model=read_model,
                    dependencies=extra,
                    name=name if index == 0 else f"{name}:trailing-slash",
                    route_class_override=route_class,
                )
            if action == "list" and resource.query_fields["group"]:
                router.add_api_route(
                    ":groups",
                    self._group_endpoint(dependency),
                    methods=["GET"],
                    response_model=_Groups,
                    response_model_exclude_unset=True,
                    dependencies=extra,
                    name=f"{name.removesuffix('list')}groups",
                    route_class_override=route_class,
                )
        check_route_conflicts(iter_route_contexts(router.routes), all_routes=True)
        return router

    async def _project(
        self, resource: ModelResource[ModelT], obj: ModelT, read: type[BaseModel]
    ) -> BaseModel:
        await resource.ensure_loaded(obj, resource.readable)
        return read.model_validate(
            {
                name: resource.specs[name].serialize(getattr(obj, name))
                for name in resource.readable
            },
            by_name=True,
            by_alias=False,
        )

    def _limit(self, params: _CollectionParams) -> int:
        return min(params.limit or self.default_limit, self.max_limit)

    def _endpoint(
        self, action: RouterMethod, dependency: Any, read: type[BaseModel], has_retrieve: bool
    ) -> Callable[..., Any]:
        lookup_name = self._resource.lookup_name
        annotation = self._resource.specs[lookup_name].annotation
        key_type: Any = Annotated[annotation, Path()]
        if action == "list":

            async def list_rows(
                request: Request, params: Annotated[_ListParams, Query()], ctx: Context = dependency
            ) -> dict[str, Any]:
                raw = parameters(
                    request.query_params.multi_items(),
                    {"q", "filter", "ordering", "limit", "offset", "includeTotal"},
                )
                resource = self.get_resource(ctx)
                result = await resource.list(
                    collection=resource.collection(raw),
                    limit=self._limit(params),
                    offset=params.offset,
                    include_total=params.include_total,
                )
                return {
                    "items": [await self._project(resource, row, read) for row in result.items],
                    "limit": result.limit,
                    "offset": result.offset,
                    "has_more": result.has_more,
                    "total": result.total,
                }

            return list_rows

        if action in {"create", "patch"}:
            body = _body_schema(self._resource, action)
            names = {name: to_camel(name) for name in body.model_fields}

            async def create_row(
                request: Request, response: Response, data: ApiRequest, ctx: Context = dependency
            ) -> BaseModel:
                request.state.api_names = names
                resource = self.get_resource(ctx)
                obj = await resource.create(data)
                if has_retrieve:
                    await resource.ensure_loaded(obj, (lookup_name,))
                    identity = quote(str(getattr(obj, lookup_name)), safe="")
                    response.headers["Location"] = f"{request.url.path.rstrip('/')}/{identity}"
                return await self._project(resource, obj, read)

            async def change_row(
                request: Request, id: Any, data: ApiRequest, ctx: Context = dependency
            ) -> BaseModel:
                request.state.api_names = names
                resource = self.get_resource(ctx)
                obj = await resource.get_object(id, action=None)
                return await self._project(resource, await resource.update(obj, data), read)

            if action == "create":
                create_row.__annotations__["data"] = body
                return create_row
            change_row.__annotations__.update(id=key_type, data=body)
            return change_row

        if action == "delete":

            async def delete_row(id: Any, ctx: Context = dependency) -> Response:
                resource = self.get_resource(ctx)
                await resource.destroy(await resource.get_object(id, action=None))
                return Response(status_code=204)

            delete_row.__annotations__["id"] = key_type
            return delete_row

        async def read_row(request: Request, id: Any, ctx: Context = dependency) -> BaseModel:
            parameters(request.query_params.multi_items())
            resource = self.get_resource(ctx)
            return await self._project(resource, await resource.get_object(id), read)

        read_row.__annotations__["id"] = key_type
        return read_row

    def _group_endpoint(self, dependency: Any) -> Callable[..., Any]:
        async def list_groups(
            request: Request, params: Annotated[_GroupParams, Query()], ctx: Context = dependency
        ) -> dict[str, Any]:
            raw = parameters(
                request.query_params.multi_items(),
                {"group", "q", "filter", "limit", "offset", "includeTotal"},
            )
            resource = self.get_resource(ctx)
            state = resource.collection(raw)
            if not state.group:
                raise InvalidQuery("Choose at least one group field.")
            by = [to_camel(term["field"]) for term in state.group]
            result = await resource.groups(
                state,
                limit=self._limit(params),
                offset=params.offset,
                include_total=params.include_total,
            )
            return {
                "by": by,
                "items": [
                    {"key": dict(zip(by, row[:-1], strict=True)), "count": row[-1]}
                    for row in result.items
                ],
                "limit": result.limit,
                "offset": result.offset,
                "has_more": result.has_more,
                **({"total": result.total} if params.include_total else {}),
            }

        return list_groups
