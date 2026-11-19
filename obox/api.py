"""Typed model routes and permission dependencies for native FastAPI applications."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from types import GenericAlias, MappingProxyType
from typing import Annotated, Any, Generic, Literal, TypeVar, cast
from urllib.parse import quote
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.routing import RouteContext, iter_route_contexts
from pydantic import BaseModel, Field, create_model
from sqlalchemy.orm import InstanceState, Mapper, undefer

from obox._context import Context as Context
from obox._dependencies import _APIContext, api_context
from obox._exceptions import BuilderValidationError, NotFound
from obox._query import invalid, parameters
from obox._routes import ApiRoute
from obox._schemas import ApiRequest as ApiRequest
from obox._schemas import ApiResponse as ApiResponse
from obox.core.exceptions import ImproperlyConfigured
from obox.db._fields import ModelField, _model_value, inspect_fields, normalize
from obox.views.generic.detail import _model_label

ModelT = TypeVar("ModelT")
RouterMethod = Literal["list", "retrieve", "create", "update", "patch", "delete"]

_ROUTES: dict[RouterMethod, tuple[str, str, int, str]] = {
    "list": ("GET", "", 200, "view"),
    "retrieve": ("GET", "/{id}", 200, "view"),
    "create": ("POST", "", 201, "add"),
    "update": ("PUT", "/{id}", 200, "change"),
    "patch": ("PATCH", "/{id}", 200, "change"),
    "delete": ("DELETE", "/{id}", 204, "delete"),
}

__all__ = ["ApiRequest", "ApiResponse", "BaseApi", "Context", "ModelApi", "RouterMethod"]


class BaseApi:
    @staticmethod
    def permission_required(permission: str | None = None) -> _APIContext:
        """Require an active identity and optionally a named permission."""
        return api_context(permission=permission, csrf=True)


class _ModelRoute(ApiRoute):
    cache_control = "private, no-store"


class _ListParams(ApiRequest):
    limit: int
    offset: int = Field(default=0, ge=0, le=2**31 - 1)
    include_total: bool = False


class _Page(ApiResponse):
    limit: int
    offset: int
    has_more: bool
    total: int | None


def _check_route_conflicts(routes: Iterable[RouteContext], *, all_routes: bool = False) -> None:
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
    schema: type[BaseModel], specs: Mapping[str, ModelField], *, write: bool = False
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for name, field in schema.model_fields.items():
        spec = specs.get(name)
        if spec is None or not spec.exposed or (write and spec.readonly):
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
    include_router_methods: Sequence[RouterMethod] | None = None
    permissions: Mapping[RouterMethod, str | None] = MappingProxyType({})
    read_schema: type[ApiResponse] | None = None
    create_schema: type[ApiRequest] | None = None
    update_schema: type[ApiRequest] | None = None
    patch_schema: type[ApiRequest] | None = None
    search_fields: Sequence[str] = ()
    ordering_fields: Sequence[str] = ()
    default_limit: int = 50
    max_limit: int = 100

    def build_router(self) -> APIRouter:
        model, prefix = getattr(self, "model", None), getattr(self, "prefix", None)
        if (
            not isinstance(model, type)
            or not isinstance(prefix, str)
            or not re.fullmatch(r"/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", prefix)
        ):
            raise ImproperlyConfigured("Define a mapped model and an absolute API prefix.")
        specs = inspect_fields(model)
        mapper: Mapper[Any] = sa.inspect(model)
        pk = mapper.get_property_by_column(mapper.primary_key[0]).key
        if pk not in specs or mapper.primary_key[0].type.python_type not in (int, str, UUID):
            raise ImproperlyConfigured("ModelApi requires an int, str or UUID primary key.")
        methods = tuple(
            _ROUTES if self.include_router_methods is None else self.include_router_methods
        )
        if (
            len(methods) != len(set(methods))
            or (set(methods) | self.permissions.keys()) - _ROUTES.keys()
        ):
            raise ImproperlyConfigured("Use unique, supported ModelApi method names.")
        if not 1 <= self.default_limit <= self.max_limit < 2**31 - 1:
            raise ImproperlyConfigured("Choose positive API page limits in ascending order.")

        read = self.read_schema if set(methods) - {"delete"} else None
        names, readable = {}, {}
        if set(methods) - {"delete"}:
            if not isinstance(read, type) or not issubclass(read, ApiResponse):
                raise ImproperlyConfigured(
                    "Define an ApiResponse read_schema for routes returning records."
                )
            names = _schema_names(read, specs)
            readable = {name: specs[name] for name in read.model_fields}
        if "list" in methods:
            self._check_list_fields(readable)

        router = APIRouter(prefix=prefix, tags=[prefix.rsplit("/", 1)[-1]])
        self.configure_router(router)
        label, model_name = _model_label(model)
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
            write_names = _schema_names(body, specs, write=True) if body is not None else {}
            dependency = Depends(
                self.permission_required(
                    self.permissions.get(action, f"{label}.{permission_action}_{model_name}")
                )
            )
            name = f"{type(self).__name__}_{action}"
            endpoint = self._endpoint(
                action,
                dependency,
                body,
                write_names,
                specs[pk],
                readable,
                names,
                "retrieve" in methods,
            )
            output: Any = read if action != "delete" else None
            if action == "list":
                output = create_model(
                    f"{type(self).__name__}Page",
                    __base__=_Page,
                    items=(cast("type[Any]", GenericAlias(list, read)), ...),
                )
            router.add_api_route(
                path,
                endpoint,
                methods=[verb],
                status_code=status,
                response_model=output,
                name=name,
                route_class_override=_ModelRoute,
            )
        _check_route_conflicts(iter_route_contexts(router.routes), all_routes=True)
        return router

    def _check_list_fields(self, readable: Mapping[str, ModelField]) -> None:
        if not all(isinstance(name, str) for name in (*self.search_fields, *self.ordering_fields)):
            raise ImproperlyConfigured("Use Python field names for search and ordering.")
        for name in normalize(self.search_fields, self.model, readable):
            if not isinstance(readable[name].column.type, sa.String) or isinstance(
                readable[name].column.type, sa.Enum
            ):
                raise ImproperlyConfigured(f"Search requires a text field: {name!r}.")
        for name in normalize(self.ordering_fields, self.model, readable):
            if isinstance(readable[name].column.type, sa.JSON):
                raise ImproperlyConfigured(f"Cannot order by field {name!r}.")

    def _endpoint(
        self,
        action: RouterMethod,
        dependency: Any,
        body: type[ApiRequest] | None,
        write_names: dict[str, str],
        pk: ModelField,
        readable: dict[str, ModelField],
        names: dict[str, str],
        has_retrieve: bool,
    ) -> Callable[..., Any]:
        options = [
            undefer(getattr(self.model, name))
            for name, spec in readable.items()
            if "obox_files" not in spec.column.info
        ]

        async def project(ctx: Context, obj: ModelT) -> ApiResponse:
            state = cast("InstanceState[ModelT]", sa.inspect(obj))
            missing = state.unloaded.intersection(readable)
            if missing:
                await ctx.session.refresh(obj, attribute_names=list(missing))
            schema = cast("type[ApiResponse]", self.read_schema)
            return schema.model_validate(
                {name: spec.serialize(getattr(obj, name)) for name, spec in readable.items()},
                by_name=True,
                by_alias=False,
            )

        async def lookup(ctx: Context, key: Any) -> ModelT:
            stmt = self.get_queryset(ctx, action=action).where(pk.column == key)
            if action != "delete":
                stmt = stmt.options(*options)
            if action in {"update", "patch", "delete"}:
                stmt = stmt.with_for_update(of=self.model)
            obj = await ctx.session.scalar(stmt)
            if obj is None:
                raise NotFound("Record is unavailable.")
            return obj

        def payload(request: Request, data: ApiRequest) -> dict[str, Any]:
            request.state.api_names = write_names
            fields = data.model_fields_set if action == "patch" else write_names
            return {name: getattr(data, name) for name in fields if name in write_names}

        if action == "list":
            query_fields: dict[str, Any] = {
                "limit": (int, Field(default=self.default_limit, ge=1, le=self.max_limit)),
            }
            if self.search_fields:
                query_fields["search"] = (str | None, Field(default=None, max_length=256))
            if self.ordering_fields:
                query_fields["order"] = (str | None, Field(default=None, max_length=1024))
            query = create_model(
                f"{type(self).__name__}ListQuery", __base__=_ListParams, **query_fields
            )

            async def list_rows(
                request: Request, params: _ListParams, ctx: Context = dependency
            ) -> Any:
                parameters(
                    request.query_params.multi_items(), {"offset", "includeTotal", *query_fields}
                )
                stmt = self.get_queryset(ctx, action="list").options(*options)
                search = getattr(params, "search", None)
                if search:
                    stmt = stmt.where(
                        sa.or_(
                            *(
                                getattr(self.model, name).icontains(search, autoescape=True)
                                for name in self.search_fields
                            )
                        )
                    )
                stmt = stmt.order_by(None).order_by(
                    *self._ordering(getattr(params, "order", None), pk, names)
                )
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

        if action == "create":

            async def create_row(
                request: Request, response: Response, data: ApiRequest, ctx: Context = dependency
            ) -> ApiResponse:
                obj = await self.create(ctx, payload(request, data))
                await ctx.session.flush()
                if has_retrieve:
                    response.headers["Location"] = (
                        f"{request.url.path}/{quote(str(getattr(obj, pk.name)), safe='')}"
                    )
                return await project(ctx, obj)

            create_row.__annotations__["data"] = body
            return create_row

        if action in {"update", "patch"}:
            mutate = self.patch if action == "patch" else self.update

            async def change_row(
                request: Request, id: Any, data: ApiRequest, ctx: Context = dependency
            ) -> ApiResponse:
                obj = await lookup(ctx, id)
                obj = await mutate(ctx, obj, payload(request, data))
                await ctx.session.flush()
                return await project(ctx, obj)

            change_row.__annotations__.update(id=pk.annotation, data=body)
            return change_row

        if action == "delete":

            async def delete_row(id: Any, ctx: Context = dependency) -> Response:
                await self.delete(ctx, await lookup(ctx, id))
                return Response(status_code=204)

            delete_row.__annotations__["id"] = pk.annotation
            return delete_row

        async def read_row(request: Request, id: Any, ctx: Context = dependency) -> ApiResponse:
            parameters(request.query_params.multi_items())
            return await project(ctx, await self.retrieve(ctx, await lookup(ctx, id)))

        read_row.__annotations__["id"] = pk.annotation
        return read_row

    def _ordering(self, raw: str | None, pk: ModelField, names: Mapping[str, str]) -> list[Any]:
        allowed = {names[name]: name for name in self.ordering_fields}
        seen: set[str] = set()
        ordering = []
        if raw is not None:
            for term in raw.split(","):
                alias, separator, direction = term.partition(".")
                name = allowed.get(alias)
                if name is None or name in seen or (separator and direction not in {"asc", "desc"}):
                    raise invalid("order", "Use unique sortable fields with .asc or .desc.")
                seen.add(name)
                column = getattr(self.model, name)
                ordering.append(
                    (column.desc() if direction == "desc" else column.asc()).nulls_last()
                )
        if pk.name not in seen:
            ordering.append(pk.column.asc())
        return ordering

    async def _write_values(
        self, ctx: Context, values: dict[str, Any], obj: ModelT | None = None
    ) -> dict[str, Any]:
        mapper = cast("Mapper[ModelT]", sa.inspect(self.model))
        state = cast("InstanceState[ModelT]", sa.inspect(obj)) if obj is not None else None
        values = dict(values)
        with ctx.session.no_autoflush:
            for name in values.keys() & mapper.column_attrs.keys():
                column = mapper.column_attrs[name].columns[0]
                value = values[name]
                if value is None and not column.nullable and not column.type.should_evaluate_none:
                    raise BuilderValidationError({name: ["This field cannot be null."]})
                try:
                    values[name] = _model_value(column, value)
                except (TypeError, ValueError) as exc:
                    raise BuilderValidationError({name: ["Invalid model value."]}) from exc
            for table in mapper.tables:
                if not isinstance(table, sa.Table):
                    continue
                for fk in table.foreign_key_constraints:
                    names = [mapper.get_property_by_column(key.parent).key for key in fk.elements]
                    written = values.keys() & set(names)
                    if not written:
                        continue
                    if state is not None and (
                        missing := state.unloaded.intersection(names) - written
                    ):
                        await ctx.session.refresh(obj, attribute_names=list(missing))
                    keys = [
                        values[name] if name in values else getattr(obj, name, None)
                        for name in names
                    ]
                    if (
                        None not in keys
                        and await ctx.session.scalar(
                            sa.select(sa.literal(True))
                            .where(
                                *(
                                    key.column == value
                                    for key, value in zip(fk.elements, keys, strict=True)
                                )
                            )
                            .limit(1)
                        )
                        is None
                    ):
                        raise BuilderValidationError(
                            {min(written): ["Referenced record is unavailable."]}
                        )
            fields = [
                name
                for name in values.keys() & mapper.relationships.keys()
                if "obox_files" in mapper.relationships[name].info
            ]
            if fields:
                files = ctx._owner._feature("files")
                if files is None:
                    raise ImproperlyConfigured("Install Files before writing attachments.")
                values = await files.resolve(self.model, values, ctx, obj)
                if state is not None and (unloaded := state.unloaded.intersection(fields)):
                    await ctx.session.refresh(obj, attribute_names=list(unloaded))
        return values

    def configure_router(self, router: APIRouter) -> None:
        """Register extra FastAPI endpoints before the generated routes."""

    def get_queryset(self, ctx: Context, *, action: RouterMethod) -> sa.Select[tuple[ModelT]]:
        return sa.select(self.model)

    async def list(
        self,
        ctx: Context,
        queryset: sa.Select[tuple[ModelT]],
        *,
        limit: int,
        offset: int,
        include_total: bool,
    ) -> tuple[Sequence[ModelT], bool, int | None]:
        total = None
        if include_total:
            count = sa.select(sa.func.count()).select_from(queryset.order_by(None).subquery())
            total = (await ctx.session.scalar(count)) or 0
        rows = (await ctx.session.scalars(queryset.offset(offset).limit(limit + 1))).all()
        return rows[:limit], len(rows) > limit, total

    async def retrieve(self, ctx: Context, obj: ModelT) -> ModelT:
        return obj

    async def create(self, ctx: Context, values: dict[str, Any]) -> ModelT:
        values = await self._write_values(ctx, values)
        columns = cast("Mapper[ModelT]", sa.inspect(self.model)).column_attrs
        for name, value in values.items():
            prop = columns.get(name)
            if value is None and prop is not None and not prop.columns[0].type.should_evaluate_none:
                values[name] = sa.null()
        obj = self.model(**values)
        ctx.session.add(obj)
        return obj

    async def _update(self, ctx: Context, obj: ModelT, values: dict[str, Any]) -> ModelT:
        for name, value in (await self._write_values(ctx, values, obj)).items():
            setattr(obj, name, value)
        return obj

    async def update(self, ctx: Context, obj: ModelT, values: dict[str, Any]) -> ModelT:
        return await self._update(ctx, obj, values)

    async def patch(self, ctx: Context, obj: ModelT, values: dict[str, Any]) -> ModelT:
        return await self._update(ctx, obj, values)

    async def delete(self, ctx: Context, obj: ModelT) -> None:
        await ctx.session.delete(obj)
