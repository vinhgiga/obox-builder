"""Typed model routes for native FastAPI applications."""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial, update_wrapper
from inspect import Parameter, isfunction, signature
from types import GenericAlias, MappingProxyType
from typing import (
    Annotated,
    Any,
    ClassVar,
    Generic,
    Literal,
    ParamSpec,
    TypeVar,
    cast,
    get_type_hints,
)
from urllib.parse import quote

from fastapi import APIRouter, Path, Query, Request, Response
from fastapi.params import Depends as Dependency
from fastapi.routing import RouteContext, iter_route_contexts
from pydantic import BaseModel, ConfigDict, Field, SkipValidation, create_model
from pydantic.alias_generators import to_camel
from starlette.routing import compile_path

from obox.components.query import parameters
from obox.core.exceptions import DomainError, ImproperlyConfigured, InvalidQuery
from obox.db.fields import schema
from obox.db.filters import MAX_FILTER_LENGTH
from obox.db.identity import model_label
from obox.resources import ModelResource
from obox.runtime.context import Context as Context
from obox.web.dependencies import PUBLIC as PUBLIC
from obox.web.dependencies import (
    READ_METHODS,
    PermissionPolicy,
    api_principal_context,
    permission_names,
)
from obox.web.dependencies import api_context as api_context
from obox.web.routes import ApiRoute
from obox.web.schemas import ApiRequest as ApiRequest
from obox.web.schemas import ApiResponse as ApiResponse

ModelT = TypeVar("ModelT")
P = ParamSpec("P")
R = TypeVar("R")
RouterMethod = Literal["list", "retrieve", "create", "patch", "delete"]

_ROUTES: dict[RouterMethod, tuple[str, str, int, str]] = {
    "list": ("GET", "", 200, "view"),
    "retrieve": ("GET", "/{id}", 200, "view"),
    "create": ("POST", "", 201, "add"),
    "patch": ("PATCH", "/{id}", 200, "change"),
    "delete": ("DELETE", "/{id}", 204, "delete"),
}

__all__ = [
    "PUBLIC",
    "ApiRequest",
    "ApiResponse",
    "Context",
    "ModelApi",
    "RouterMethod",
    "api_context",
    "route",
]


class _ModelRoute(ApiRoute):
    cache_control = "private, no-store"


@dataclass(frozen=True)
class _RouteSpec:
    method: str
    path: str
    permission: PermissionPolicy
    options: dict[str, Any]


@dataclass(frozen=True)
class _Verb:
    method: str

    def __call__(
        self, path: str, *, permission: PermissionPolicy, **options: Any
    ) -> Callable[[Callable[P, R]], Callable[P, R]]:
        permission_names(permission, allow_public=self.method in READ_METHODS)
        if (
            not isinstance(path, str)
            or (path and not path.startswith("/"))
            or any(char in path for char in "?#\\")
        ):
            raise ImproperlyConfigured("Provide a ModelApi route path relative to its prefix.")
        try:
            _, path_format, converters = compile_path(path)
        except (AssertionError, ValueError) as exc:
            raise ImproperlyConfigured(f"Invalid ModelApi route path: {path}.") from exc
        for name in converters:
            path_format = path_format.replace(f"{{{name}}}", "", 1)
        if "{" in path_format or "}" in path_format:
            raise ImproperlyConfigured(f"Invalid ModelApi route path: {path}.")
        if {"methods", "route_class_override"}.intersection(options):
            raise ImproperlyConfigured("The ModelApi decorator selects the method and route class.")

        def decorate(method: Callable[P, R]) -> Callable[P, R]:
            if not isfunction(method) or "__obox_route__" in method.__dict__:
                raise ImproperlyConfigured("Use one route decorator per ModelApi instance method.")
            method.__dict__["__obox_route__"] = _RouteSpec(self.method, path, permission, options)
            return method

        return decorate


class _Routes:
    get = _Verb("GET")
    post = _Verb("POST")
    put = _Verb("PUT")
    patch = _Verb("PATCH")
    delete = _Verb("DELETE")


route = _Routes()


def _custom_endpoint(
    method: Callable[..., Any], owner: type[Any], dependency: Dependency
) -> Callable[..., Any]:
    try:
        hints = get_type_hints(method, localns=dict(vars(owner)), include_extras=True)
    except NameError as exc:
        raise ImproperlyConfigured(
            f"Cannot resolve annotations for {method.__qualname__}."
        ) from exc
    original = signature(method)
    params = []
    for parameter in original.parameters.values():
        annotation = hints.get(parameter.name, parameter.annotation)
        if annotation is Context and parameter.default is Parameter.empty:
            annotation = Annotated[Context, dependency]
        params.append(parameter.replace(annotation=annotation))
    endpoint = update_wrapper(partial(method), method)
    endpoint.__dict__["__signature__"] = original.replace(
        parameters=params, return_annotation=hints.get("return", original.return_annotation)
    )
    return endpoint


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
    """Declare CRUD and custom routes for a ModelResource."""

    prefix: str
    slug: str | None = None
    resource: type[ModelResource[ModelT]]
    actions: Collection[RouterMethod] | None = None
    permissions: Mapping[RouterMethod, PermissionPolicy] = MappingProxyType({})
    dependencies: Sequence[Any] = ()
    principal_dependency: Callable[..., Any] | None = None
    default_limit: int = 50
    max_limit: int = 100
    tags: Sequence[str] | None = None
    name_prefix: str | None = None
    error_status: Mapping[type[DomainError], int] = MappingProxyType({})
    _custom_routes: ClassVar[tuple[tuple[str, type[Any], _RouteSpec], ...]] = ()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        actions = cls.actions
        if actions is not None and (
            isinstance(actions, str)
            or not isinstance(actions, Collection)
            or any(not isinstance(action, str) or action not in _ROUTES for action in actions)
            or len(actions) != len(set(actions))
        ):
            raise ImproperlyConfigured("Use unique, supported ModelApi action names.")
        if not isinstance(cls.permissions, Mapping) or cls.permissions.keys() - _ROUTES.keys():
            raise ImproperlyConfigured("Use supported ModelApi actions as permission keys.")
        for action, permission in cls.permissions.items():
            permission_names(permission, allow_public=_ROUTES[action][0] in READ_METHODS)
        if (
            type(cls.default_limit) is not int
            or type(cls.max_limit) is not int
            or not 1 <= cls.default_limit <= cls.max_limit < 2**31 - 1
        ):
            raise ImproperlyConfigured("Choose positive API page limits in ascending order.")
        if not isinstance(cls.error_status, Mapping) or any(
            not isinstance(error, type)
            or not issubclass(error, DomainError)
            or type(status) is not int
            or not 400 <= status <= 599
            for error, status in cls.error_status.items()
        ):
            raise ImproperlyConfigured("Map domain errors to HTTP error statuses.")
        members = {
            name: (owner, member)
            for owner in reversed(cls.__mro__)
            for name, member in vars(owner).items()
        }
        if reserved := _ROUTES.keys() & members.keys():
            raise ImproperlyConfigured(
                f"Reserved ModelApi action names: {', '.join(sorted(reserved))}."
            )
        custom = []
        for name, (owner, member) in members.items():
            spec = getattr(getattr(member, "__func__", member), "__obox_route__", None)
            if spec is not None:
                if not isfunction(member):
                    raise ImproperlyConfigured("ModelApi routes must decorate instance methods.")
                custom.append((name, owner, spec))
        cls._custom_routes = tuple(custom)

    @classmethod
    def as_router(cls, prefix: str | None = None) -> APIRouter:
        return cls().build_router(prefix)

    def build_router(self, prefix: str | None = None) -> APIRouter:
        resource = getattr(self, "resource", None)
        if not isinstance(resource, type) or not issubclass(resource, ModelResource):
            raise ImproperlyConfigured("Define a ModelResource class.")
        return self.build_resource_router(
            prefix if prefix is not None else getattr(self, "prefix", ""), resource()
        )

    def build_resource_router(self, prefix: str, resource: ModelResource[ModelT]) -> APIRouter:
        if not isinstance(prefix, str) or not re.fullmatch(
            r"/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", prefix
        ):
            raise ImproperlyConfigured("Provide an absolute API prefix.")
        actions = _ROUTES if self.actions is None else self.actions
        route_class = (
            type("ModelRoute", (_ModelRoute,), {"error_status_overrides": dict(self.error_status)})
            if self.error_status
            else _ModelRoute
        )
        if "create" in actions:
            resource.check_create(resource.writable)
        model_name = resource.model.__name__
        read = schema(f"{model_name}Read", resource.specs, resource.readable, base=ApiResponse)
        router = APIRouter(
            prefix=prefix,
            tags=list(self.tags) if self.tags is not None else [prefix.rsplit("/", 1)[-1]],
            dependencies=list(self.dependencies),
            route_class=route_class,
        )
        resolver = type(self).principal_dependency
        if resolver is not None and not callable(resolver):
            raise ImproperlyConfigured("Choose a callable principal dependency.")
        context = api_principal_context(resolver) if resolver is not None else None

        def require(permission: PermissionPolicy) -> Dependency:
            policy = api_context(permission=permission, csrf=True)
            return Dependency(policy.with_context(context) if context is not None else policy)

        if self._custom_routes:
            custom_route_class = cast(
                "type[_ModelRoute]",
                type("CustomModelRoute", (route_class,), {"record_contract": False}),
            )
            for name, owner, spec in self._custom_routes:
                dependency = require(spec.permission)
                options = spec.options.copy()
                options.setdefault("name", f"obox:api:{prefix}:custom:{name}")
                options["dependencies"] = [dependency, *(options.get("dependencies") or ())]
                router.add_api_route(
                    spec.path,
                    _custom_endpoint(getattr(self, name), owner, dependency),
                    methods=[spec.method],
                    route_class_override=custom_route_class,
                    **options,
                )

        label, permission_model = model_label(resource.model)
        for action, (verb, path, status, operation) in _ROUTES.items():
            if action not in actions:
                continue
            dependency = require(
                self.permissions.get(action, f"{label}.{operation}_{permission_model}")
            )
            name = (
                f"{self.name_prefix}-{action}"
                if self.name_prefix
                else f"obox:api:{prefix}:{action}"
            )
            read_model: Any = None if action == "delete" else read
            if action == "list":
                items = cast("type[Any]", GenericAlias(list, read))
                read_model = create_model(f"{model_name}Page", __base__=_Page, items=(items, ...))
            endpoint = self._endpoint(resource, action, dependency, read, "retrieve" in actions)
            for index, route_path in enumerate((path,) if path else ("", "/")):
                router.add_api_route(
                    route_path,
                    endpoint,
                    methods=[verb],
                    status_code=status,
                    response_model=read_model,
                    name=name if index == 0 else f"{name}:trailing-slash",
                )
            if action == "list" and resource.query_fields["group"]:
                router.add_api_route(
                    ":groups",
                    self._group_endpoint(resource, dependency),
                    methods=["GET"],
                    response_model=_Groups,
                    response_model_exclude_unset=True,
                    name=f"{name.removesuffix('list')}groups",
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
        self,
        definition: ModelResource[ModelT],
        action: RouterMethod,
        dependency: Any,
        read: type[BaseModel],
        has_retrieve: bool,
    ) -> Callable[..., Any]:
        lookup_name = definition.lookup_name
        annotation = definition.specs[lookup_name].annotation
        key_type: Any = Annotated[annotation, Path()]
        if action == "list":

            async def list_rows(
                request: Request, params: Annotated[_ListParams, Query()], ctx: Context = dependency
            ) -> dict[str, Any]:
                raw = parameters(
                    request.query_params.multi_items(),
                    {"q", "filter", "ordering", "limit", "offset", "includeTotal"},
                )
                resource = definition.bind(ctx.session, ctx.principal)
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
            body = _body_schema(definition, action)
            names = {name: to_camel(name) for name in body.model_fields}

            async def create_row(
                request: Request, response: Response, data: ApiRequest, ctx: Context = dependency
            ) -> BaseModel:
                request.state.api_names = names
                resource = definition.bind(ctx.session, ctx.principal)
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
                resource = definition.bind(ctx.session, ctx.principal)
                obj = await resource.get_object(id, action=None)
                return await self._project(resource, await resource.update(obj, data), read)

            if action == "create":
                create_row.__annotations__["data"] = body
                return create_row
            change_row.__annotations__.update(id=key_type, data=body)
            return change_row

        if action == "delete":

            async def delete_row(id: Any, ctx: Context = dependency) -> Response:
                resource = definition.bind(ctx.session, ctx.principal)
                await resource.destroy(await resource.get_object(id, action=None))
                return Response(status_code=204)

            delete_row.__annotations__["id"] = key_type
            return delete_row

        async def read_row(request: Request, id: Any, ctx: Context = dependency) -> BaseModel:
            parameters(request.query_params.multi_items())
            resource = definition.bind(ctx.session, ctx.principal)
            return await self._project(resource, await resource.get_object(id), read)

        read_row.__annotations__["id"] = key_type
        return read_row

    def _group_endpoint(
        self, definition: ModelResource[ModelT], dependency: Any
    ) -> Callable[..., Any]:
        async def list_groups(
            request: Request, params: Annotated[_GroupParams, Query()], ctx: Context = dependency
        ) -> dict[str, Any]:
            raw = parameters(
                request.query_params.multi_items(),
                {"group", "q", "filter", "limit", "offset", "includeTotal"},
            )
            resource = definition.bind(ctx.session, ctx.principal)
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
