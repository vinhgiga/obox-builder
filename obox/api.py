"""Typed model routes and permission dependencies for native FastAPI applications."""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from copy import copy
from types import GenericAlias, MappingProxyType
from typing import Annotated, Any, Generic, Literal, TypeVar, cast
from urllib.parse import quote
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.routing import RouteContext, iter_route_contexts
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SkipValidation, create_model
from sqlalchemy.orm import Mapper

from obox.components.query import invalid, parameters, parse_expression
from obox.core.async_utils import invoke
from obox.core.exceptions import DomainError, ImproperlyConfigured
from obox.core.pagination import Paginator
from obox.core.principal import valid_permission
from obox.db.fields import ModelField, normalize, schema, validate_text
from obox.db.filters import FilterTerm, compile_filter, compile_predicate
from obox.db.identity import model_label
from obox.db.query import group_query, scope_query, search_query
from obox.resources import ModelResource, resolve_resource
from obox.runtime.context import Context as Context
from obox.web.dependencies import _APIContext, api_context, api_principal_context
from obox.web.routes import ApiRoute
from obox.web.schemas import ApiRequest as ApiRequest
from obox.web.schemas import ApiResponse as ApiResponse

ModelT = TypeVar("ModelT")
RouterMethod = Literal["list", "retrieve", "create", "update", "patch", "delete"]
Action = Literal["list", "retrieve", "create", "update", "partial_update", "destroy"]
ReadT = TypeVar("ReadT", bound=BaseModel)

_ROUTES: dict[RouterMethod, tuple[str, str, int, str]] = {
    "list": ("GET", "", 200, "view"),
    "retrieve": ("GET", "/{id}", 200, "view"),
    "create": ("POST", "", 201, "add"),
    "update": ("PUT", "/{id}", 200, "change"),
    "patch": ("PATCH", "/{id}", 200, "change"),
    "delete": ("DELETE", "/{id}", 204, "delete"),
}
_ACTIONS: dict[Action, RouterMethod] = {
    "list": "list",
    "retrieve": "retrieve",
    "create": "create",
    "update": "update",
    "partial_update": "patch",
    "destroy": "delete",
}

__all__ = ["ApiRequest", "ApiResponse", "BaseApi", "Context", "ModelApi", "PageOut", "RouterMethod"]


class BaseApi:
    @staticmethod
    def permission_required(permission: str | None = None) -> _APIContext:
        """Require an active identity and optionally a named permission."""
        return api_context(permission=permission, csrf=True)


class _ModelRoute(ApiRoute):
    cache_control = "private, no-store"


class _QueryParams(ApiRequest):
    # parameters() validates the original keys and detects repeated query parameters.
    model_config = ConfigDict(extra="ignore")


class _ListParams(_QueryParams):
    limit: int
    offset: int = Field(default=0, ge=0, le=2**31 - 1)
    include_total: bool = False


class _NumberedListParams(_QueryParams):
    page: int
    page_size: int


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
    items: list[_Group]
    total: int | None = None


class _GroupParams(_ListParams):
    by: str = Field(description="One to three unique groupable response aliases.")
    order: Literal["key.asc", "key.desc", "count.asc", "count.desc"] = "key.asc"


def _capped_int(maximum: int) -> Any:
    return Annotated[int, AfterValidator(lambda value: min(value, maximum))]


class PageOut(BaseModel, Generic[ReadT]):
    items: list[ReadT]
    page: int
    page_size: int
    total: int
    pages: int


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


def _schema_names(
    schema: type[BaseModel],
    specs: Mapping[str, ModelField],
    *,
    write: bool = False,
    relations: Collection[str] = (),
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for name, field in schema.model_fields.items():
        spec = specs.get(name)
        if name not in relations and (
            spec is None or not spec.exposed or (write and spec.readonly)
        ):
            raise ImproperlyConfigured(f"Invalid field {name!r} in {schema.__name__}.")
        alias = field.validation_alias if write else field.serialization_alias
        alias = name if alias is None else alias
        if not isinstance(alias, str) or not alias or alias in aliases.values():
            raise ImproperlyConfigured(f"Use unique string aliases in {schema.__name__}.")
        aliases[name] = alias
    if not write:
        for name, computed in schema.model_computed_fields.items():
            alias = computed.alias or name
            if alias in aliases.values():
                raise ImproperlyConfigured(f"Use unique string aliases in {schema.__name__}.")
            aliases[name] = alias
    return aliases


class ModelApi(BaseApi, Generic[ModelT]):
    """Generate protected CRUD routes from explicit request and response schemas."""

    model: type[ModelT]
    prefix: str
    slug: str | None = None
    resource: type[ModelResource[ModelT]] | None = None
    _resource: ModelResource[ModelT]
    _generated: set[str]
    include_router_methods: Sequence[RouterMethod] | None = None
    actions: Collection[Action] | None = None
    permissions: Mapping[RouterMethod, str] = MappingProxyType({})
    read_schema: type[BaseModel] | None = None
    create_schema: type[ApiRequest] | None = None
    update_schema: type[ApiRequest] | None = None
    patch_schema: type[ApiRequest] | None = None
    dependencies: Sequence[Any] = ()
    principal_dependency: Callable[..., Any] | None = None
    search_fields: Sequence[str] | None = None
    ordering_fields: Sequence[str] | None = None
    filter_fields: Sequence[str] | None = None
    group_fields: Sequence[str] | None = None
    _search_fields: tuple[str, ...]
    _ordering_fields: dict[str, ModelField]
    _filter_fields: dict[str, ModelField]
    _group_fields: dict[str, ModelField]
    default_limit: int = 50
    max_limit: int = 100
    pagination: Literal["offset", "page"] = "offset"
    page_param = "page"
    page_size_param = "page_size"
    max_page_size = 100
    lookup_url_kwarg = "id"
    lookup_type: type[Any] | None = None
    tags: Sequence[str] | None = None
    name_prefix: str | None = None
    error_status: Mapping[type[DomainError], int] = MappingProxyType({})

    @classmethod
    def as_router(
        cls,
        prefix: str | None = None,
        *,
        tags: Sequence[str] | None = None,
        name_prefix: str | None = None,
    ) -> APIRouter:
        api = cls()
        if tags is not None:
            api.tags = tags
        if name_prefix is not None:
            api.name_prefix = name_prefix
        return api.build_router(prefix)

    def get_resource(self, ctx: Context) -> ModelResource[ModelT]:
        resources = ctx.session.info.setdefault("obox.resources", {})
        # A session may be reused with a different identity. The resource's
        # authorization hooks must never see a previous request's principal.
        key = (id(self._resource), id(ctx.principal))
        if key not in resources:
            resources[key] = self._resource.bind(ctx.session, ctx.principal)
        return cast(ModelResource[ModelT], resources[key])

    def build_router(self, prefix: str | None = None) -> APIRouter:
        binding = copy(self)
        model = getattr(self, "model", None)
        if self.resource is not None and model is not None:
            raise ImproperlyConfigured("Specify either model or resource.")
        target = self.resource or model
        if target is None:
            raise ImproperlyConfigured("Define a ModelResource or SQLAlchemy model.")
        resource = resolve_resource(target)
        if self.resource is None:
            explicit_fields = tuple(
                dict.fromkeys(
                    name
                    for declaration in (
                        self.read_schema,
                        self.create_schema,
                        self.patch_schema,
                        self.update_schema,
                    )
                    if declaration is not None
                    for name in declaration.model_fields
                )
            )
            if explicit_fields:
                resource = type("ApiResource", (ModelResource,), {"fields": explicit_fields})(
                    model=resource.model
                )
        return binding.build_resource_router(
            prefix if prefix is not None else getattr(self, "prefix", ""), resource
        )

    def build_resource_router(self, prefix: str, resource: ModelResource[ModelT]) -> APIRouter:
        if not re.fullmatch(r"/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", prefix):
            raise ImproperlyConfigured("Provide an absolute API prefix.")
        if self.include_router_methods is not None and (
            isinstance(self.include_router_methods, str)
            or not isinstance(self.include_router_methods, Sequence)
        ):
            raise ImproperlyConfigured("Choose a sequence of ModelApi methods.")
        if self.pagination not in {"offset", "page"}:
            raise ImproperlyConfigured("Choose offset or page pagination.")
        methods: tuple[RouterMethod, ...]
        if self.actions is not None:
            if self.include_router_methods is not None or not isinstance(self.actions, Collection):
                raise ImproperlyConfigured("Choose either actions or include_router_methods.")
            if (
                isinstance(self.actions, str)
                or any(
                    not isinstance(action, str) or action not in _ACTIONS for action in self.actions
                )
                or len(self.actions) != len(set(self.actions))
            ):
                raise ImproperlyConfigured("Use unique, supported ModelApi action names.")
            methods = tuple(method for action, method in _ACTIONS.items() if action in self.actions)
        elif self.include_router_methods is not None:
            methods = tuple(self.include_router_methods)
        elif self.pagination == "page":
            methods = ("list", "retrieve")
        else:
            methods = tuple(_ROUTES)
        if not all(isinstance(action, str) and action in _ROUTES for action in methods) or len(
            methods
        ) != len(set(methods)):
            raise ImproperlyConfigured("Use unique, supported ModelApi method names.")
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
        if self.pagination == "page" and (
            type(self.max_page_size) is not int
            or not 1 <= self.max_page_size < 2**31 - 1
            or any(
                not isinstance(name, str) or not name.isidentifier()
                for name in (self.page_param, self.page_size_param)
            )
            or self.page_param == self.page_size_param
            or {self.page_param, self.page_size_param} & {"search", "order", "filter"}
        ):
            raise ImproperlyConfigured(
                "Choose valid, distinct page parameters and a positive limit."
            )
        if not isinstance(self.lookup_url_kwarg, str) or not self.lookup_url_kwarg.isidentifier():
            raise ImproperlyConfigured("Choose a valid lookup URL parameter.")
        if self.lookup_type is not None and self.lookup_type not in (int, str, UUID):
            raise ImproperlyConfigured("Choose an int, str or UUID lookup type.")
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
        specs = resource.specs
        pk = resource.pk_name
        lookup = specs[resource.lookup_name]
        selected = {name: specs[name] for name in resource.readable}
        writable = resource.writable
        if self.read_schema is None:
            self.read_schema = schema(
                f"{self.model.__name__}Read", specs, resource.readable, base=ApiResponse
            )
        generated: set[str] = set()
        self._generated = generated
        for operation in ("create", "update", "patch"):
            attr = f"{operation}_schema"
            if operation not in methods or getattr(self, attr) is not None:
                continue
            if (
                operation == "update"
                and self.actions is None
                and self.include_router_methods is not None
            ):
                continue
            definition = resource.get_schema("create" if operation == "create" else "update")
            fields: dict[str, Any] = {}
            for name in writable:
                if name not in definition.model_fields:
                    continue
                info = definition.model_fields[name]
                annotation: Any = info.annotation
                if info.metadata:
                    annotation = Annotated[annotation, *info.metadata]
                default = ... if operation != "patch" and info.is_required() else None
                fields[name] = (Annotated[annotation, SkipValidation], default)
            setattr(
                self,
                attr,
                create_model(
                    f"{self.model.__name__}{operation.title()}", __base__=ApiRequest, **fields
                ),
            )
            generated.add(operation)
        if "create" in methods and "create" in generated:
            resource.check_create(writable)
        read = self.read_schema if set(methods) - {"delete"} else None
        names, readable, related = {}, {}, set()
        if set(methods) - {"delete"}:
            if not isinstance(read, type) or not issubclass(read, BaseModel):
                raise ImproperlyConfigured(
                    "Define a Pydantic read_schema for routes returning records."
                )
            if read.model_config.get("from_attributes") is not True:
                raise ImproperlyConfigured("Set from_attributes=True on the read_schema.")
            relationships = cast("Mapper[ModelT]", sa.inspect(self.model)).relationships.keys()
            related = (read.model_fields.keys() & set(relationships)) - specs.keys()
            if related and not resource.load_options:
                raise ImproperlyConfigured(
                    "Declare ModelResource.load_options for API relationships."
                )
            names = _schema_names(read, selected, relations=related)
            readable = {name: specs[name] for name in read.model_fields if name not in related}
        if "list" in methods:
            self._bind_list_fields(readable, names)

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
            policy = self.permission_required(permission)
            return Depends(policy.with_context(context) if context is not None else policy)

        self.configure_router(router)
        label, model_name = model_label(self.model)
        schemas = {
            "create": self.create_schema,
            "update": self.update_schema,
            "patch": self.patch_schema,
        }
        for action in methods:
            verb, path, status, permission_action = _ROUTES[action]
            body = schemas.get(action)
            if action in schemas and not (
                isinstance(body, type)
                and issubclass(body, ApiRequest)
                and body.model_config.get("extra") == "forbid"
                and body.model_config.get("validate_by_name") is False
                and body.model_config.get("validate_by_alias") is True
            ):
                raise ImproperlyConfigured(
                    f"Define an ApiRequest {action}_schema with strict field names."
                )
            write_names = _schema_names(body, selected, write=True) if body is not None else {}
            if not write_names.keys() <= set(writable):
                raise ImproperlyConfigured("API fields must be writable on the ModelResource.")
            permission = f"{label}.{permission_action}_{model_name}"
            dependency = require(permission)
            additional = self.permissions.get(action)
            extra = [require(additional)] if additional else None
            name = (
                f"{self.name_prefix}-{action}"
                if self.name_prefix
                else f"obox:api:{prefix}:{action}"
            )
            endpoint = self._endpoint(
                action,
                dependency,
                body,
                write_names,
                specs[pk],
                lookup,
                readable,
                related,
                "retrieve" in methods,
                action in generated,
            )
            output: Any = read if action != "delete" else None
            if action == "list":
                output = (
                    PageOut.__class_getitem__(cast("type[BaseModel]", read))
                    if self.pagination == "page"
                    else create_model(
                        f"{type(self).__name__}Page",
                        __base__=_Page,
                        items=(cast("type[Any]", GenericAlias(list, read)), ...),
                    )
                )
            route_path = path.replace("{id}", "{" + self.lookup_url_kwarg + "}")
            route_paths = (route_path, "/") if not route_path else (route_path,)
            for index, route_path in enumerate(route_paths):
                router.add_api_route(
                    route_path,
                    endpoint,
                    methods=[verb],
                    status_code=status,
                    response_model=output,
                    response_model_by_alias=True,
                    dependencies=extra,
                    name=name if index == 0 else f"{name}:trailing-slash",
                    route_class_override=route_class,
                )
            if action == "list" and self._group_fields:
                router.add_api_route(
                    ":groups",
                    self._group_endpoint(dependency, specs[pk]),
                    methods=["GET"],
                    response_model=_Groups,
                    response_model_exclude_unset=True,
                    dependencies=extra,
                    name=f"{name.removesuffix('list')}groups",
                    route_class_override=route_class,
                )
        check_route_conflicts(iter_route_contexts(router.routes), all_routes=True)
        return router

    def _bind_list_fields(
        self, readable: Mapping[str, ModelField], names: Mapping[str, str]
    ) -> None:
        searchable, sortable, filterable, groupable = {}, {}, {}, {}
        for name, field in readable.items():
            type_ = field.column.type
            if "obox_files" in field.column.info:
                filterable[name] = field
                continue
            if isinstance(type_, sa.JSON):
                continue
            filterable[name] = sortable[name] = field
            if isinstance(type_, sa.String) and not isinstance(type_, sa.Enum):
                searchable[name] = field
            if isinstance(type_, sa.String | sa.Boolean | sa.Integer | sa.Date):
                groupable[name] = field

        def fields(
            attr: str, configured: Sequence[str] | None, available: Mapping[str, ModelField]
        ) -> tuple[str, ...]:
            if configured is None:
                return tuple(available)
            if (
                not isinstance(configured, Sequence)
                or isinstance(configured, str)
                or not all(isinstance(name, str) for name in configured)
            ):
                raise ImproperlyConfigured(f"Use sequences of Python field names for {attr}.")
            normalize(
                tuple(name[1:] if name.startswith(("^", "=")) else name for name in configured)
                if attr == "search_fields"
                else configured,
                self.model,
                available,
            )
            return tuple(configured)

        def aliases(
            attr: str, configured: Sequence[str] | None, available: Mapping[str, ModelField]
        ) -> dict[str, ModelField]:
            return {names[name]: available[name] for name in fields(attr, configured, available)}

        self._search_fields = fields("search_fields", self.search_fields, searchable)
        self._ordering_fields = aliases("ordering_fields", self.ordering_fields, sortable)
        self._filter_fields = aliases("filter_fields", self.filter_fields, filterable)
        self._group_fields = aliases("group_fields", self.group_fields, groupable)

    def _predicate_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if self._search_fields:
            fields["search"] = (
                str | None,
                Field(default=None, max_length=256, description="Match all of up to ten words."),
            )
        if self._filter_fields:
            fields["filter"] = (
                str | None,
                Field(
                    default=None, max_length=4000, description="Filter using field.operator.value."
                ),
            )
        return fields

    def _limit_field(self) -> tuple[Any, Any]:
        return (
            _capped_int(self.max_limit),
            Field(
                default=self.default_limit,
                ge=1,
                description=f"Result limit. Values above {self.max_limit} are capped.",
            ),
        )

    async def _filtered_queryset(
        self, ctx: Context, params: _QueryParams
    ) -> sa.Select[tuple[ModelT]]:
        stmt = scope_query(await invoke(self.get_queryset, ctx, action="list"), self.model)
        if (raw := getattr(params, "filter", None)) is not None:
            dialect = ctx.session.get_bind().dialect.name

            def predicate(term: FilterTerm) -> Any:
                spec = self._filter_fields.get(term.field)
                if spec is None:
                    raise invalid(term.field, "Unknown filter field.", "unknown_field")
                return compile_predicate(
                    spec, getattr(self.model, spec.name), term, dialect, rest=True
                )

            _, expression, _ = compile_filter(parse_expression(raw), predicate)
            stmt = stmt.where(expression)
        if search := getattr(params, "search", None):
            if len(search.split()) > 10:
                raise invalid("search", "Supply at most ten search words.")
            try:
                validate_text(search)
            except ValueError as exc:
                raise invalid("search", str(exc)) from exc
            stmt = search_query(stmt, self.model, self._search_fields, search)
        return cast("sa.Select[tuple[ModelT]]", stmt)

    def _endpoint(
        self,
        action: RouterMethod,
        dependency: Any,
        body: type[ApiRequest] | None,
        write_names: dict[str, str],
        pk: ModelField,
        lookup_field: ModelField,
        readable: dict[str, ModelField],
        related: set[str],
        has_retrieve: bool,
        generated: bool,
    ) -> Callable[..., Any]:
        async def project(ctx: Context, obj: ModelT) -> BaseModel:
            await self.get_resource(ctx).ensure_loaded(obj, (*readable, *related))
            schema = cast("type[BaseModel]", self.read_schema)
            return schema.model_validate(
                {
                    **{name: spec.serialize(getattr(obj, name)) for name, spec in readable.items()},
                    **{name: getattr(obj, name) for name in related},
                },
                by_name=True,
                by_alias=False,
                from_attributes=True,
            )

        async def lookup(ctx: Context, key: Any) -> ModelT:
            stmt = await invoke(self.get_queryset, ctx, action=action)
            return await self.get_resource(ctx).get_object(
                key,
                queryset=stmt,
                action="destroy"
                if action == "delete"
                else "update"
                if action == "patch"
                else action,
                for_update=action in {"update", "patch", "delete"},
            )

        def payload(request: Request, data: ApiRequest) -> dict[str, Any]:
            request.state.api_names = write_names
            fields = data.model_fields_set if generated or action == "patch" else write_names
            return {name: getattr(data, name) for name in fields if name in write_names}

        if action == "list":
            numbered = self.pagination == "page"
            query_fields = self._predicate_fields()
            if numbered:
                size = min(self._resource.page_size or self.default_limit, self.max_page_size)
                query_fields.update(
                    {
                        "page": (int, Field(default=1, ge=1, alias=self.page_param)),
                        "page_size": (
                            _capped_int(self.max_page_size),
                            Field(
                                default=size,
                                ge=1,
                                alias=self.page_size_param,
                                description=f"Page size, capped at {self.max_page_size}.",
                            ),
                        ),
                    }
                )
                allowed = {self.page_param, self.page_size_param}
            else:
                query_fields["limit"] = self._limit_field()
                allowed = {"limit", "offset", "includeTotal"}
            if self._ordering_fields:
                query_fields["order"] = (
                    str | None,
                    Field(
                        default=None,
                        max_length=1024,
                        description="Up to five comma-separated field aliases with .asc or .desc.",
                    ),
                )
            allowed.update(query_fields.keys() - {"page", "page_size"})
            query = create_model(
                f"{type(self).__name__}ListQuery",
                __base__=_NumberedListParams if numbered else _ListParams,
                **query_fields,
            )

            async def list_rows(
                request: Request,
                params: _ListParams | _NumberedListParams,
                ctx: Context = dependency,
            ) -> Any:
                parameters(request.query_params.multi_items(), allowed)
                stmt = await self._filtered_queryset(ctx, params)
                order = getattr(params, "order", None)
                if not numbered or order is not None or not stmt._order_by_clauses:
                    stmt = stmt.order_by(None).order_by(*self._ordering(order, pk))
                if isinstance(params, _NumberedListParams):
                    page = await self.get_resource(ctx).paginate(
                        params.page,
                        page_size=params.page_size,
                        queryset=stmt,
                    )
                    return {
                        "items": [await project(ctx, row) for row in page],
                        "page": page.number,
                        "page_size": page.page_size,
                        "total": page.total,
                        "pages": page.num_pages,
                    }
                limit = params.limit
                rows, more, total = await self.list(
                    ctx, stmt, limit=limit, offset=params.offset, include_total=params.include_total
                )
                return {
                    "items": [await project(ctx, row) for row in rows],
                    "limit": limit,
                    "offset": params.offset,
                    "has_more": more,
                    "total": total,
                }

            list_rows.__annotations__["params"] = Annotated[query, Query()]
            return list_rows

        annotation = self.lookup_type or lookup_field.annotation
        key_type: Any = Annotated[annotation, Path(alias=self.lookup_url_kwarg)]
        if action == "create":

            async def create_row(
                request: Request, response: Response, data: ApiRequest, ctx: Context = dependency
            ) -> BaseModel:
                obj = await self.create(ctx, payload(request, data))
                await ctx.session.flush()
                if has_retrieve:
                    await self.get_resource(ctx).ensure_loaded(obj, (lookup_field.name,))
                    response.headers["Location"] = (
                        f"{request.url.path}/{quote(str(getattr(obj, lookup_field.name)), safe='')}"
                    )
                return await project(ctx, obj)

            create_row.__annotations__["data"] = body
            return create_row

        if action in {"update", "patch"}:
            mutate = self.patch if action == "patch" else self.update

            async def change_row(
                request: Request, id: Any, data: ApiRequest, ctx: Context = dependency
            ) -> BaseModel:
                obj = await lookup(ctx, id)
                obj = await mutate(ctx, obj, payload(request, data))
                await ctx.session.flush()
                return await project(ctx, obj)

            change_row.__annotations__.update(id=key_type, data=body)
            return change_row

        if action == "delete":

            async def delete_row(id: Any, ctx: Context = dependency) -> Response:
                await self.delete(ctx, await lookup(ctx, id))
                return Response(status_code=204)

            delete_row.__annotations__["id"] = key_type
            return delete_row

        async def read_row(request: Request, id: Any, ctx: Context = dependency) -> BaseModel:
            parameters(request.query_params.multi_items())
            return await project(ctx, await self.retrieve(ctx, await lookup(ctx, id)))

        read_row.__annotations__["id"] = key_type
        return read_row

    def _ordering(self, raw: str | None, pk: ModelField) -> Sequence[Any]:
        seen: set[str] = set()
        if raw is None:
            return self._resource.get_ordering() or (pk.column.asc(),)
        terms = raw.split(",")
        if len(terms) > 5:
            raise invalid("order", "Supply at most five sort fields.")
        ordering = []
        for term in terms:
            alias, separator, direction = term.partition(".")
            if (
                not alias
                or alias.startswith("-")
                or any(char.isspace() for char in alias)
                or alias in seen
                or (separator and direction not in {"asc", "desc"})
            ):
                raise invalid("order", "Use unique sortable fields with .asc or .desc.")
            field = self._ordering_fields.get(alias)
            if field is None:
                raise invalid(alias, "Unknown sort field.", "unknown_field")
            seen.add(alias)
            column = getattr(self.model, field.name)
            ordering.append((column.desc() if direction == "desc" else column.asc()).nulls_last())
        if all(self._ordering_fields[alias].name != pk.name for alias in seen):
            ordering.append(pk.column.asc())
        return ordering

    def _group_endpoint(self, dependency: Any, pk: ModelField) -> Callable[..., Any]:
        query_fields = self._predicate_fields()
        query_fields["limit"] = self._limit_field()
        allowed = {"by", "order", "offset", "includeTotal", *query_fields}
        query = create_model(
            f"{type(self).__name__}GroupQuery", __base__=_GroupParams, **query_fields
        )

        async def list_groups(
            request: Request, params: _GroupParams, ctx: Context = dependency
        ) -> dict[str, Any]:
            parameters(request.query_params.multi_items(), allowed)
            by = params.by.split(",")
            if not 1 <= len(by) <= 3 or len(by) != len(set(by)) or "" in by:
                raise invalid("by", "Supply one to three unique group fields.")
            columns = []
            for alias in by:
                field = self._group_fields.get(alias)
                if field is None:
                    raise invalid(alias, "Unknown group field.", "unknown_field")
                columns.append(getattr(self.model, field.name))

            resource = self.get_resource(ctx)
            scoped = await resource._list_query(await self._filtered_queryset(ctx, params))
            stmt = group_query(scoped, pk.column, columns, order=params.order)
            rows = (
                await ctx.session.execute(stmt.offset(params.offset).limit(params.limit + 1))
            ).all()
            result = {
                "by": by,
                "items": [
                    {"key": dict(zip(by, row[:-1], strict=True)), "count": row[-1]}
                    for row in rows[: params.limit]
                ],
                "limit": params.limit,
                "offset": params.offset,
                "has_more": len(rows) > params.limit,
            }
            if params.include_total:
                result["total"] = await Paginator.count(ctx.session, stmt)
            return result

        list_groups.__annotations__["params"] = Annotated[query, Query()]
        return list_groups

    def configure_router(self, router: APIRouter) -> None:
        """Register extra FastAPI endpoints before the generated routes."""

    def get_queryset(self, ctx: Context, *, action: RouterMethod) -> sa.Select[tuple[ModelT]]:
        return self.get_resource(ctx).get_queryset()

    async def list(
        self,
        ctx: Context,
        queryset: sa.Select[tuple[ModelT]],
        *,
        limit: int,
        offset: int,
        include_total: bool,
    ) -> tuple[Sequence[ModelT], bool, int | None]:
        result = await self.get_resource(ctx).list(
            queryset=queryset,
            limit=limit,
            offset=offset,
            include_total=include_total,
        )
        return result.items, result.has_more, result.total

    async def retrieve(self, ctx: Context, obj: ModelT) -> ModelT:
        return obj

    async def create(self, ctx: Context, values: dict[str, Any]) -> ModelT:
        return await self.get_resource(ctx).create(
            values,
            validate="create" in self._generated
            or self._resource.schema is not None
            or self._resource.create_schema is not None,
        )

    async def update(self, ctx: Context, obj: ModelT, values: dict[str, Any]) -> ModelT:
        return await self.get_resource(ctx).update(obj, values, partial=self.actions is None)

    async def patch(self, ctx: Context, obj: ModelT, values: dict[str, Any]) -> ModelT:
        if self.actions is not None:
            return await self.get_resource(ctx).update(obj, values, partial=True)
        return await self.update(ctx, obj, values)

    async def delete(self, ctx: Context, obj: ModelT) -> None:
        await self.get_resource(ctx).destroy(obj)
