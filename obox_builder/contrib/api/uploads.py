"""Credential-bound staged uploads and guarded private downloads."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import re
import time
from copy import copy
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, create_model
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile as StoredUpload

from obox_builder.conf import settings
from obox_builder.contrib.api.deps import DB, USER, principal
from obox_builder.contrib.api.problems import Problem
from obox_builder.contrib.uploads import open_upload, persist_uploads, stage_uploads
from obox_builder.core.storage import default_storage
from obox_builder.exceptions import PermissionDenied
from obox_builder.resources import ReadOnlyModelResource

STAGED = re.compile(r"admin/api-staged/[0-9a-f]{32}\.(?:jpg|png|webp|pdf|txt)")


def _signature(payload: str) -> str:
    return hmac.new(
        settings.SECRET_KEY.get_secret_value().encode(), payload.encode(), hashlib.sha256
    ).hexdigest()


def _claim(request: Request, resource_name: str, field: str, path: str) -> str:
    data = [
        principal(request),
        resource_name,
        field,
        path,
        int(time.time()) + settings.API_UPLOAD_TTL_SECONDS,
    ]
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
    return f"{payload}.{_signature(payload)}"


def _path(request: Request, resource_name: str, field: str, token: str) -> str:
    try:
        payload, signature = token.rsplit(".", 1)
        if len(token) > 2048 or not hmac.compare_digest(signature, _signature(payload)):
            raise ValueError
        owner, label, name, relative, expiry = json.loads(
            base64.b64decode(payload, altchars=b"-_", validate=True)
        )
        if (
            (owner, label, name) != (principal(request), resource_name, field)
            or expiry <= time.time()
            or not STAGED.fullmatch(relative)
        ):
            raise ValueError
        if not default_storage.exists(relative):
            raise ValueError
        return relative
    except (ValueError, TypeError, KeyError) as exc:
        raise Problem(
            422,
            "validation_error",
            "Upload claim is invalid or expired.",
            errors={field: ["Upload again."]},
        ) from exc


async def prepare_files(
    request: Request, resource: ReadOnlyModelResource, data: dict[str, Any]
) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    prepared = dict(data)
    uploads = []
    for spec in resource.specs.values():
        if spec.type.name != "file" or spec.name not in data or data[spec.name] is None:
            continue
        value = data[spec.name]
        if (
            not isinstance(value, dict)
            or set(value) != {"upload_token"}
            or not isinstance(value["upload_token"], str)
        ):
            raise Problem(
                422,
                "validation_error",
                "File fields require an upload_token.",
                errors={spec.name: ["Upload a file first."]},
            )
        path = _path(request, resource.name, spec.name, value["upload_token"])
        limit = settings.UPLOAD_MAX_BYTES - request.state.api_upload_bytes
        with default_storage.open(path) as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise Problem(413, "payload_too_large", "Total uploads exceed the request limit.")
        request.state.api_upload_bytes += len(content)
        upload = StoredUpload(io.BytesIO(content), filename=PurePosixPath(path).name)
        values, staged, errors = await stage_uploads({spec.name: upload}, [spec])
        if errors:
            raise Problem(422, "validation_error", "Invalid upload.", errors=errors)
        prepared[spec.name] = values[spec.name]
        uploads.extend(staged)
    return prepared, uploads


def purge_staged(*, limit: int) -> int:
    directory = Path(settings.MEDIA_ROOT) / "admin" / "api-staged"
    cutoff = time.time() - settings.API_UPLOAD_TTL_SECONDS
    removed = 0
    if directory.is_dir():
        for path in directory.iterdir():
            if removed >= limit:
                break
            if (
                STAGED.fullmatch(f"admin/api-staged/{path.name}")
                and path.is_file()
                and path.stat().st_mtime < cutoff
            ):
                path.unlink(missing_ok=True)
                removed += 1
    return removed


class UploadReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upload_token: str = Field(min_length=1, max_length=2048)


class UploadClaim(BaseModel):
    upload_token: str
    expires_in: int


def input_schema(
    resource: ReadOnlyModelResource, schema: type[BaseModel] | None
) -> type[BaseModel] | None:
    if schema is None:
        return None
    fields: dict[str, Any] = {
        name: (info.annotation, copy(info)) for name, info in schema.model_fields.items()
    }
    converted = False
    for name, info in schema.model_fields.items():
        spec = resource.specs.get(name)
        if spec is not None and spec.type.name == "file":
            annotation = (
                UploadReference | None
                if spec.nullable or not info.is_required()
                else UploadReference
            )
            fields[name] = (annotation, copy(info))
            converted = True
    return (
        create_model(schema.__name__ + "Uploads", __config__=schema.model_config, **fields)
        if converted
        else schema
    )


def register_uploads(router: APIRouter) -> None:
    @router.post("/uploads", response_model=UploadClaim, status_code=201)
    async def upload(
        request: Request,
        user: USER,
        db: DB,
        resource: str = Form(...),
        field: str = Form(...),
        file: UploadFile = File(...),
        id: str | None = Form(None),
    ) -> UploadClaim:
        from obox_builder.contrib.api.routing import operation_context

        form = await request.form()
        if set(form) - {"resource", "field", "file", "id"} or any(
            len(form.getlist(key)) != 1 for key in form
        ):
            raise Problem(422, "validation_error", "Unknown or repeated upload fields.")
        binding = request.state.api_site.bindings.get(resource)
        definition = request.state.api_registry.resources.get(resource)
        if binding is None or not isinstance(definition, ReadOnlyModelResource):
            raise LookupError("Resource is unavailable.")
        ctx = operation_context(request).for_resource(definition)
        operation = "create" if id is None else "update"
        spec = definition.operations.get(operation)
        if spec is None or not ctx.can(spec.permission, effect=spec.effect):
            raise PermissionDenied("Writing files is not allowed.")
        obj = None if id is None else await definition.resolve_target(ctx, spec, id)
        if not definition.can_execute(ctx, operation, obj):
            raise PermissionDenied("Writing this record is not allowed.")
        published = binding.adapter.input_schemas.get(operation, spec.input_schema)
        field_spec = definition.specs.get(field)
        if (
            field_spec is None
            or field_spec.type.name != "file"
            or not field_spec.is_editable()
            or published is None
            or field not in published.model_fields
            or not definition.field_allowed(ctx, field_spec, write=True)
        ):
            raise LookupError("File field is unavailable.")
        if field_spec.editable_if is not None and not field_spec.editable_if(
            ctx.record_context(obj)
        ):
            raise PermissionDenied("This field cannot be written.")
        values, staged, errors = await stage_uploads({field: file}, [field_spec])
        if errors:
            raise Problem(422, "validation_error", "Invalid upload.", errors=errors)
        if not staged:
            raise Problem(422, "validation_error", "A non-empty file is required.")
        relative = values[field].replace("admin/uploads/", "admin/api-staged/")
        persist_uploads([(relative, staged[0][1])])
        request.state.api_uploads.append((relative, b""))
        return UploadClaim(
            upload_token=_claim(request, resource, field, relative),
            expires_in=settings.API_UPLOAD_TTL_SECONDS,
        )


def register_download(router: APIRouter, resource: ReadOnlyModelResource, path: str) -> None:
    @router.get(
        path + "/{pk}/files/{field}",
        response_class=StreamingResponse,
        responses={
            200: {
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                }
            }
        },
    )
    async def download(request: Request, pk: str, field: str, user: USER, db: DB):
        from obox_builder.contrib.api.routing import operation_context

        ctx = operation_context(request).for_resource(resource)
        record = await resource.execute("get", ctx, target=pk)
        spec = resource.specs.get(field)
        if spec is None or spec.type.name != "file" or field not in record.model_fields_set:
            raise LookupError("File is unavailable.")
        relative = getattr(record, field)
        stream = open_upload(relative) if isinstance(relative, str) else None
        if stream is None:
            raise LookupError("File is unavailable.")
        return StreamingResponse(
            iter(lambda: stream.read(65536), b""),
            media_type="application/octet-stream",
            background=BackgroundTask(stream.close),
            headers={
                "Content-Disposition": f'attachment; filename="{PurePosixPath(relative).name}"',
                "X-Content-Type-Options": "nosniff",
            },
        )
