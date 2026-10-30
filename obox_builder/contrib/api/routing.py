"""Compile typed HTTP endpoints from frozen resource operation metadata."""

from __future__ import annotations

import inspect
from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, RootModel, create_model

from obox_builder.conf import settings
from obox_builder.contrib.api import idempotency
from obox_builder.contrib.api.deps import current_user, principal
from obox_builder.contrib.api.etag import check_if_match, etag
from obox_builder.contrib.api.problems import Problem, ProblemDocument, ProblemRoute
from obox_builder.contrib.api.ratelimit import MemoryRateLimiter
from obox_builder.contrib.api.site import Api, ApiBinding
from obox_builder.exceptions import ImproperlyConfigured, PermissionDenied
from obox_builder.resources import OperationContext, QueryInput, ReadOnlyModelResource
from obox_builder.resources.base import Resource
from obox_builder.resources.operations import OperationSpec

QueryParameters = create_model("QueryParameters", __base__=QueryInput, filter=(str | None, None))


def build_router(api: Api) -> APIRouter:
    limiter = api.rate_limiter or MemoryRateLimiter()
    catalog = api.registry

    async def initialize(request: Request) -> None:
        request.state.api_site = api
        request.state.api_registry = catalog
        request.state.api_limiter = limiter
        if request.url.path.endswith("/auth/login"):
            peer = request.client.host if request.client else "unknown"
            request.state.api_rate_headers = await limiter.check(
                f"login:{peer}", settings.API_RATE_LIMIT
            )
        origin = request.headers.get("origin")
        if origin and origin in settings.API_TRUSTED_ORIGINS:
            request.state.api_cors_origin = origin

    router = APIRouter(
        route_class=ProblemRoute,
        dependencies=[Depends(initialize)],
        responses={
            status: {
                "description": HTTPStatus(status).phrase,
                "content": {
                    "application/problem+json": {"schema": ProblemDocument.model_json_schema()}
                },
            }
            for status in (400, 401, 403, 404, 409, 412, 413, 415, 422, 428, 429, 500)
        },
    )
    from obox_builder.contrib.api.auth import register_auth

    register_auth(router)
    from obox_builder.contrib.api.tokens import register_tokens

    register_tokens(router)
    from obox_builder.contrib.api.uploads import register_download, register_uploads

    register_uploads(router)
    for resource_name, binding in sorted(
        api.bindings.items(), key=lambda item: item[1].path.count("/"), reverse=True
    ):
        resource = catalog.get(resource_name)
        if type(resource) is not binding.resource:
            raise ImproperlyConfigured("API binding does not match the resource registry.")
        referenced = (
            set(binding.adapter.exclude_operations)
            | binding.adapter.methods.keys()
            | binding.adapter.input_schemas.keys()
            | binding.adapter.output_schemas.keys()
        )
        unknown = referenced - resource.operations.keys()
        if unknown:
            raise ImproperlyConfigured(f"Unknown API operations: {sorted(unknown)}.")
        for spec in resource.operations.values():
            if spec.name not in binding.adapter.exclude_operations:
                register_operation(router, api, binding, resource, spec)
        if isinstance(resource, ReadOnlyModelResource) and any(
            spec.type.name == "file" for spec in resource.specs.values()
        ):
            register_download(router, resource, binding.path)

    @router.options("/{path:path}", include_in_schema=False)
    async def preflight(request: Request, path: str) -> Response:
        origin = request.headers.get("origin")
        if not origin or origin not in settings.API_TRUSTED_ORIGINS:
            raise Problem(403, "csrf_failed", "Origin is not trusted.")
        request.state.api_cors_origin = origin
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": (
                    "Authorization, Content-Type, X-CSRF-Token, If-Match, Idempotency-Key"
                ),
            },
        )

    @router.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PATCH", "DELETE", "PUT", "HEAD"],
        include_in_schema=False,
    )
    async def missing(path: str) -> None:
        raise Problem(404, "not_found", "Endpoint is unavailable.")

    return router


def operation_context(request: Request) -> OperationContext:
    token = getattr(request.state, "api_token", None)
    return OperationContext(
        actor=request.state.actor,
        db=request.state.api_db,
        registry=request.state.api_registry,
        scopes=tuple(token.scopes) if token is not None else None,
        metadata={
            "source": "api",
            "namespace": request.state.api_site.name,
            "request_id": getattr(request.state, "request_id", None),
        },
    )


def register_operation(
    router: APIRouter, api: Api, binding: ApiBinding, resource: Resource, spec: OperationSpec
) -> None:
    builtin = spec.method if spec.builtin else ""
    methods = {"list": "GET", "get": "GET", "create": "POST", "update": "PATCH", "delete": "DELETE"}
    method = methods.get(builtin, "POST")
    method = binding.adapter.methods.get(spec.name, method).upper()
    if (
        method not in {methods.get(builtin, "POST"), "GET"}
        or method == "GET"
        and spec.effect != "read"
    ):
        raise ImproperlyConfigured("Only read operations may use GET.")
    input_schema = binding.adapter.input_schemas.get(spec.name, spec.input_schema)
    output_schema = binding.adapter.output_schemas.get(spec.name, spec.output_schema)
    if builtin == "list":
        input_schema = QueryParameters
    if builtin in {"create", "update", "get"}:
        for original, custom in (
            (spec.input_schema, input_schema),
            (spec.output_schema, output_schema),
        ):
            if (
                original is not None
                and custom is not None
                and set(custom.model_fields) - original.model_fields.keys()
            ):
                raise ImproperlyConfigured(
                    "Adapter schemas cannot publish additional resource fields."
                )
    if builtin in {"create", "update"} and isinstance(resource, ReadOnlyModelResource):
        from obox_builder.contrib.api.uploads import input_schema as upload_schema

        input_schema = upload_schema(resource, input_schema)
    if spec.scope == "selection":
        fields: dict[str, Any] = {
            "ids": (
                list[str],
                Field(min_length=1, max_length=getattr(resource, "max_selection", 200)),
            )
        }
        if input_schema is not None:
            fields["data"] = (input_schema, ...)
        if spec.name in binding.adapter.etag_operations:
            fields["etags"] = (dict[str, str] | None, None)
        input_schema = create_model(
            resource.name.replace(".", "_") + spec.name + "Selection",
            __config__=ConfigDict(extra="forbid"),
            **fields,
        )
    path = binding.path
    if spec.scope == "record":
        path += "/{pk}"
    if builtin not in methods:
        path += "/actions/" + spec.name
    status = 201 if builtin == "create" else 204 if spec.output_schema is None else 200

    async def endpoint(**kwargs: Any) -> Response:
        request: Request = kwargs["request"]
        ctx = operation_context(request).for_resource(resource)
        data: Any = kwargs.get("data")
        if builtin == "list":
            data = data.model_dump(exclude_none=True)
            if data.get("filter"):
                import json

                try:
                    data["filter"] = json.loads(data["filter"])
                except ValueError as exc:
                    raise Problem(400, "invalid_query", "Invalid filter JSON.") from exc
        if not ctx.can(spec.permission, effect=spec.effect):
            raise PermissionDenied(f"Permission {spec.permission.name!r} is required.")
        target = kwargs.get("pk")
        etags = None
        if spec.scope == "selection":
            etags = getattr(data, "etags", None)
            target, data = data.ids, getattr(data, "data", None)

        async def resolve() -> Any:
            if spec.scope != "collection":
                return await resource.resolve_target(ctx, spec, target)
            return None

        async def execute() -> Response:
            resolved = await resolve()
            if (
                spec.scope == "selection"
                and spec.name in binding.adapter.etag_operations
                and isinstance(resource, ReadOnlyModelResource)
            ):
                for obj in resolved:
                    current = await resource.serialize(ctx, obj)
                    provided = (etags or {}).get(str(resource.get_pk(obj)))
                    check_if_match(
                        etag(
                            current.model_dump(mode="json", exclude_unset=True),
                            getattr(obj, "version", None),
                        ),
                        provided,
                    )
            if (
                spec.scope == "record"
                and spec.name in binding.adapter.etag_operations
                and isinstance(resource, ReadOnlyModelResource)
            ):
                current = await resource.serialize(ctx, resolved)
                check_if_match(
                    etag(
                        current.model_dump(mode="json", exclude_unset=True),
                        getattr(resolved, "version", None),
                    ),
                    request.headers.get("if-match"),
                )

            prepared = data
            if builtin in {"create", "update"} and isinstance(resource, ReadOnlyModelResource):
                from obox_builder.contrib.api.uploads import prepare_files
                from obox_builder.contrib.uploads import persist_uploads

                raw = data.model_dump(exclude_unset=True) if isinstance(data, BaseModel) else data
                prepared, staged = await prepare_files(request, resource, raw)
                persist_uploads(staged)
                request.state.api_uploads.extend(staged)
            result = await resource.execute(spec.name, ctx, prepared, target=resolved)
            original: dict[str, Any] = (
                result.model_dump(exclude_unset=True) if result is not None else {}
            )
            if result is not None and output_schema is not None:
                values = original
                if output_schema is not spec.output_schema and not issubclass(
                    output_schema, RootModel
                ):
                    values = {
                        name: value
                        for name, value in original.items()
                        if name in output_schema.model_fields
                    }
                result = output_schema.model_validate(values, by_name=True)
            payload = (
                result.model_dump(mode="json", exclude_unset=True, by_alias=True)
                if result is not None
                else None
            )
            headers: dict[str, str] = {}
            if (
                payload is not None
                and builtin in {"get", "create", "update"}
                and isinstance(resource, ReadOnlyModelResource)
            ):
                row = resolved
                if builtin == "create":
                    key = resource.pk_column().key
                    row = await ctx.session.get(resource.model, resource.parse_pk(original[key]))
                    headers["Location"] = str(request.url).rstrip("/") + "/" + str(original[key])
                canonical = (await resource.serialize(ctx, row)).model_dump(
                    mode="json", exclude_unset=True
                )
                headers["ETag"] = etag(canonical, getattr(row, "version", None))
            elif payload is not None and result is not None and hasattr(result, "version"):
                headers["ETag"] = etag(payload, result.version)
            return (
                Response(status_code=204)
                if payload is None
                else JSONResponse(payload, status_code=status, headers=headers)
            )

        if spec.effect == "read":
            return await execute()

        async def authorize_replay(payload: Any, headers: dict[str, str]) -> None:
            resolved = await resolve()
            objects = resolved if spec.scope == "selection" else [resolved]
            if not all(resource.can_execute(ctx, spec.name, obj) for obj in objects):
                raise PermissionDenied("This operation is no longer allowed.")
            if (
                isinstance(resource, ReadOnlyModelResource)
                and builtin in {"create", "update"}
                and payload
                and output_schema is not None
            ):
                row = resolved
                if builtin == "create":
                    from urllib.parse import unquote, urlsplit

                    identity = unquote(
                        urlsplit(headers.get("location", "")).path.rsplit("/", 1)[-1]
                    )
                    row = await resource.resolve_target(ctx, spec, identity)
                visible = (await resource.serialize(ctx, row)).model_dump(exclude_unset=True)
                aliases = {
                    field.serialization_alias or name: name
                    for name, field in output_schema.model_fields.items()
                }
                if {aliases.get(name, name) for name in payload} - visible.keys():
                    raise PermissionDenied(
                        "The stored response contains fields no longer available."
                    )

        body = (
            data.model_dump(mode="json", exclude_unset=True)
            if isinstance(data, BaseModel)
            else data or {}
        )
        if spec.scope == "selection":
            body = {"ids": target, "data": body, "etags": etags}
        return await idempotency.execute(
            request,
            ctx.session,
            principal(request),
            body,
            execute,
            authorize_replay=authorize_replay,
        )

    parameters = [
        inspect.Parameter("request", inspect.Parameter.KEYWORD_ONLY, annotation=Request),
        inspect.Parameter(
            "user", inspect.Parameter.KEYWORD_ONLY, default=Depends(current_user), annotation=Any
        ),
    ]
    if spec.scope == "record":
        parameters.append(inspect.Parameter("pk", inspect.Parameter.KEYWORD_ONLY, annotation=str))
    if input_schema is not None:
        parameters.append(
            inspect.Parameter(
                "data",
                inspect.Parameter.KEYWORD_ONLY,
                annotation=input_schema,
                default=Query() if method == "GET" else inspect.Parameter.empty,
            )
        )
    endpoint.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
    endpoint.__name__ = resource.name.replace(".", "_") + "_" + spec.name
    router.add_api_route(
        path,
        endpoint,
        methods=[method],
        status_code=status,
        response_model=output_schema,
        response_model_exclude_unset=True,
        operation_id=endpoint.__name__,
        tags=[resource.title],
    )
