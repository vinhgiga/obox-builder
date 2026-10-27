"""Private staged files referenced by short-lived, principal-bound claims."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import re
import time
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import APIRouter, Request
from starlette.datastructures import UploadFile

from obox_builder.conf import settings
from obox_builder.contrib.admin.api.deps import DB, USER, principal
from obox_builder.contrib.admin.api.meta import model_context
from obox_builder.contrib.admin.api.problems import Problem
from obox_builder.contrib.admin.api.serializers import api_specs
from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.services import RecordService
from obox_builder.contrib.admin.uploads import persist_uploads, stage_uploads
from obox_builder.core.storage import default_storage
from obox_builder.fields import FieldSpec

STAGED = re.compile(r"admin/api-staged/[0-9a-f]{32}\.(?:jpg|png|webp|pdf|txt)")


def _signature(payload: str) -> str:
    return hmac.new(
        settings.SECRET_KEY.get_secret_value().encode(), payload.encode(), hashlib.sha256
    ).hexdigest()


def _claim(ctx: AdminContext, field: str, path: str) -> str:
    data = [
        principal(ctx.request),
        RecordService(ctx).admin.model_label,
        field,
        path,
        int(time.time()) + settings.ADMIN_API_UPLOAD_TTL_SECONDS,
    ]
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
    return f"{payload}.{_signature(payload)}"


def _path(ctx: AdminContext, field: str, token: str) -> str:
    try:
        payload, signature = token.rsplit(".", 1)
        if len(token) > 2048 or not hmac.compare_digest(signature, _signature(payload)):
            raise ValueError
        owner, label, name, relative, expiry = json.loads(
            base64.b64decode(payload, altchars=b"-_", validate=True)
        )
        if (
            (owner, label, name)
            != (principal(ctx.request), RecordService(ctx).admin.model_label, field)
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
    ctx: AdminContext, data: dict[str, Any], specs: list[FieldSpec]
) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    prepared = dict(data)
    uploads = []
    for spec in specs:
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
        path = _path(ctx, spec.name, value["upload_token"])
        limit = settings.ADMIN_MAX_UPLOAD_BYTES - ctx.request.state.api_upload_bytes
        with default_storage.open(path) as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise Problem(413, "payload_too_large", "Total uploads exceed the request limit.")
        ctx.request.state.api_upload_bytes += len(content)
        upload = UploadFile(io.BytesIO(content), filename=PurePosixPath(path).name)
        values, staged, errors = await stage_uploads({spec.name: upload}, [spec])
        if errors:
            raise Problem(422, "validation_error", "Invalid upload.", errors=errors)
        prepared[spec.name] = values[spec.name]
        uploads.extend(staged)
    return prepared, uploads


def purge_staged(*, limit: int) -> int:
    directory = Path(settings.MEDIA_ROOT) / "admin" / "api-staged"
    cutoff = time.time() - settings.ADMIN_API_UPLOAD_TTL_SECONDS
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


def register_uploads(router: APIRouter) -> None:
    @router.post(
        "/uploads",
        status_code=201,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "multipart/form-data": {
                        "schema": {
                            "type": "object",
                            "required": ["model", "field", "file"],
                            "properties": {
                                "model": {"type": "string"},
                                "field": {"type": "string"},
                                "id": {"type": "string"},
                                "file": {"type": "string", "format": "binary"},
                            },
                        }
                    }
                },
            }
        },
    )
    async def upload(request: Request, user: USER, db: DB) -> Any:
        if request.headers.get("content-type", "").partition(";")[0] != "multipart/form-data":
            raise Problem(415, "unsupported_media_type", "A multipart upload is required.")
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > settings.ADMIN_MAX_UPLOAD_BYTES + 65536:
                raise Problem(413, "payload_too_large", "Upload is too large.")

        async def receive() -> Any:
            return {"type": "http.request", "body": bytes(content), "more_body": False}

        parsed_request = Request(request.scope, receive=receive)
        async with parsed_request.form(max_files=1, max_fields=3, max_part_size=65536) as form:
            if set(form) - {"model", "field", "file", "id"} or any(
                len(form.getlist(key)) != 1 for key in form
            ):
                raise Problem(422, "validation_error", "Unknown or repeated upload fields.")
            label, field, file = form.get("model"), form.get("field"), form.get("file")
            if (
                not isinstance(label, str)
                or not isinstance(field, str)
                or not isinstance(file, UploadFile)
            ):
                raise Problem(422, "validation_error", "model, field and file are required.")
            ctx = model_context(request, user, db, label)
            pk = form.get("id")
            if pk is not None:
                from obox_builder.contrib.admin.api.records import get_record

                if not isinstance(pk, str):
                    raise Problem(422, "validation_error", "Invalid record id.")
                ctx.obj = await get_record(ctx, pk)
            RecordService(ctx).check_permission("change" if ctx.obj is not None else "add", ctx.obj)
            spec = next(
                (
                    spec
                    for spec in api_specs(ctx)
                    if spec.name == field and spec.type.name == "file" and spec.is_editable()
                ),
                None,
            )
            if spec is None:
                raise Problem(404, "not_found", "File field is unavailable.")
            values, staged, errors = await stage_uploads({field: file}, [spec])
            if errors:
                raise Problem(422, "validation_error", "Invalid upload.", errors=errors)
            if not staged:
                raise Problem(422, "validation_error", "A non-empty filename is required.")
            relative = values[field].replace("admin/uploads/", "admin/api-staged/")
            persist_uploads([(relative, staged[0][1])])
            request.state.api_uploads.append((relative, b""))
            return {
                "upload_token": _claim(ctx, field, relative),
                "expires_in": settings.ADMIN_API_UPLOAD_TTL_SECONDS,
            }
