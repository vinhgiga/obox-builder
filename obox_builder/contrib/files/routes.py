"""Receive bounded chunks without holding a database connection over the network."""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import anyio
import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.requests import ClientDisconnect

from obox_builder.builder.http import OboxRoute
from obox_builder.contrib.files.models import File
from obox_builder.contrib.files.rules import IMAGES, VIDEOS, sniff
from obox_builder.contrib.files.schemas import (
    FileMetadata,
    NewFile,
    RenameFile,
    ReorderedFiles,
    ReorderFiles,
    UploadCreated,
)
from obox_builder.errors import BuilderValidationError, NotAuthenticated, NotFound, PermissionDenied

if TYPE_CHECKING:
    from obox_builder.context import Context
    from obox_builder.contrib.files import Files

CHUNK = 8 * 1024**2
PREVIEW = set(IMAGES.values()) | set(VIDEOS.values())


def publish(files: Files, file: File, chunk: Path) -> None:
    temporary, final = files.root / "tmp" / str(file.id), files.root / file.key
    # A previous rename may have succeeded while its database commit failed.
    if final.exists():
        os.replace(final, temporary)
    with temporary.open("r+b") as output, chunk.open("rb") as source:
        output.seek(file.offset)
        output.truncate()
        shutil.copyfileobj(source, output, length=256 * 1024)
        file.offset = output.tell()
        output.flush()
        os.fsync(output.fileno())
        file.unclaimed_at = datetime.now(UTC)
        if file.offset == file.size:
            output.seek(0)
            detected = sniff(output.read(4096))
            if file.content_type in PREVIEW and detected != file.content_type:
                file.status = "failed"
                return
            if file.content_type == "application/octet-stream" and detected:
                file.content_type = detected
    if file.offset == file.size:
        final.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.replace(temporary, final)
        file.status, file.completed_at = "ready", datetime.now(UTC)


def build_router(files: Files) -> APIRouter:
    app = files.app
    router = APIRouter(prefix=files.prefix, tags=["files"], route_class=OboxRoute)
    principal = Depends(app.settings.principal_dependency)

    def actor(who: Any) -> str:
        if (
            not getattr(who, "is_authenticated", False)
            or not getattr(who, "is_active", True)
            or who.id is None
        ):
            raise NotAuthenticated("Authentication is required.")
        return str(who.id)

    def view(file: File) -> dict[str, Any]:
        return files.metadata(file)

    async def mine(ctx: Context, fid: UUID) -> File:
        file = await ctx.session.get(File, fid)
        if file is None or file.created_by != actor(ctx.principal):
            raise NotFound("Unknown upload.")
        return file

    @router.post("", status_code=201, name="obox_files_create", response_model=UploadCreated)
    async def create(request: Request, body: NewFile, who: Any = principal) -> dict[str, Any]:
        created_by = actor(who)
        app.csrf.validate(request, who)
        owner_type, _, field = body.field.rpartition(".")
        rule = files.rule(owner_type, field)
        content_type = rule.check(body.filename, body.size)
        builder = files.builder(owner_type)
        service = app.service(builder.model)
        async with app.uow(principal=who, source="ui", request=request) as ctx:
            obj = await service._get(body.owner_id, ctx, "update") if body.owner_id else None
            action = "create" if obj is None else "update"
            await service.authorize(action, ctx, obj)
            if field not in builder.writable(ctx, action, obj=obj):
                raise PermissionDenied("File field is not writable.")
            fid = uuid4()
            file = File(
                id=fid,
                key=f"{datetime.now(UTC):%Y%m}/{fid}",
                filename=body.filename,
                content_type=content_type,
                size=body.size,
                created_by=created_by,
                owner_type=owner_type,
                field=field,
            )
            temporary = anyio.Path(files.root / "tmp" / str(fid))
            await temporary.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            await temporary.touch(mode=0o600, exist_ok=False)
            ctx.session.add(file)
        return {**view(file), "chunk_size": CHUNK}

    @router.put("/{owner_id}/order", name="obox_files_reorder", response_model=ReorderedFiles)
    async def reorder(
        owner_id: str, body: ReorderFiles, request: Request, who: Any = principal
    ) -> dict[str, Any]:
        actor(who)
        app.csrf.validate(request, who)
        owner_type, _, field = body.field.rpartition(".")
        if not owner_type:
            raise NotFound("Unknown file field.")
        files.rule(owner_type, field)
        builder = files.builder(owner_type)
        service = app.service(builder.model)
        async with app.uow(principal=who, source="ui", request=request) as ctx:
            obj = await service._get(owner_id, ctx, "update")
            if field not in builder.writable(ctx, "update", obj=obj):
                raise PermissionDenied("File field is not writable.")
            if len(set(body.ids)) != len(body.ids):
                raise BuilderValidationError({field: ["Use unique file IDs."]})
            current = set(
                (
                    await ctx.session.scalars(
                        sa.select(File.id).where(
                            File.owner_type == owner_type,
                            File.field == field,
                            File.owner_id == owner_id,
                            File.status == "ready",
                        )
                    )
                ).all()
            )
            if current != set(body.ids):
                raise BuilderValidationError(
                    {field: ["Attachment list changed. Reload and try again."]}
                )
            if body.ids:
                await ctx.session.execute(
                    sa.update(File)
                    .where(
                        File.id.in_(body.ids),
                        File.owner_type == owner_type,
                        File.field == field,
                        File.owner_id == owner_id,
                    )
                    .values(
                        position=sa.case(
                            {fid: index for index, fid in enumerate(body.ids)}, value=File.id
                        )
                    )
                )
        return {"ids": [str(fid) for fid in body.ids]}

    @router.get("/{fid}/state", name="obox_files_state", response_model=FileMetadata)
    async def state(fid: UUID, request: Request, who: Any = principal) -> JSONResponse:
        async with app.uow(principal=who, source="ui", request=request) as ctx:
            file = await ctx.session.get(File, fid)
            if file is None:
                raise NotFound("Unknown upload.")
            await files.authorize(ctx, file)
            result = view(file)
        return JSONResponse(result, headers={"Cache-Control": "private, no-store"})

    @router.patch("/{fid}", name="obox_files_append", response_model=FileMetadata)
    async def append(fid: UUID, request: Request, who: Any = principal) -> JSONResponse:
        created_by = actor(who)
        app.csrf.validate(request, who)
        if request.headers.get("content-type") != "application/offset+octet-stream":
            return JSONResponse({"detail": "Use application/offset+octet-stream."}, status_code=415)
        raw_offset = request.headers.get("upload-offset", "")
        if len(raw_offset) > 19 or not raw_offset.isascii() or not raw_offset.isdecimal():
            raise BuilderValidationError({"upload-offset": ["Send a nonnegative byte offset."]})
        offset = int(raw_offset)
        async with app.uow(principal=who, source="ui", request=request) as ctx:
            file = await mine(ctx, fid)
            if file.offset != offset or file.status != "pending" or file.owner_id is not None:
                return JSONResponse(view(file), status_code=409)
            limit = min(CHUNK, file.size - offset)
        stage = anyio.Path(files.root / "tmp" / f"{fid}.{uuid4()}")
        try:
            received = 0
            await stage.touch(mode=0o600, exist_ok=False)
            async with await stage.open("wb") as output:
                async for data in request.stream():
                    received += len(data)
                    if received > limit:
                        raise BuilderValidationError({"size": ["Chunk exceeds the upload limit."]})
                    await output.write(data)
            if received == 0:
                raise BuilderValidationError({"size": ["Send a nonempty chunk."]})
            async with app.uow(principal=who, source="ui", request=request) as ctx:
                # An UPDATE serializes competing requests on both SQLite and PostgreSQL.
                locked = await ctx.session.scalar(
                    sa.update(File)
                    .where(
                        File.id == fid,
                        File.created_by == created_by,
                        File.owner_id.is_(None),
                        File.status == "pending",
                        File.offset == offset,
                    )
                    .values(offset=File.offset)
                    .returning(File)
                )
                if locked is None:
                    return JSONResponse(view(await mine(ctx, fid)), status_code=409)
                await anyio.to_thread.run_sync(publish, files, locked, Path(stage))
                result = view(locked)
            return JSONResponse(result, status_code=409 if result["status"] == "failed" else 200)
        except ClientDisconnect:
            return JSONResponse({"offset": offset}, status_code=409)
        finally:
            with anyio.CancelScope(shield=True):
                await stage.unlink(missing_ok=True)

    @router.put("/{fid}", name="obox_files_rename", response_model=FileMetadata)
    async def rename(
        fid: UUID, body: RenameFile, request: Request, who: Any = principal
    ) -> dict[str, Any]:
        actor(who)
        app.csrf.validate(request, who)
        async with app.uow(principal=who, source="ui", request=request) as ctx:
            file = await ctx.session.get(File, fid)
            if file is None or file.status != "ready":
                raise NotFound("Unknown file.")
            await files.authorize(ctx, file)
            if file.owner_id is not None:
                builder = files.builder(file.owner_type)
                obj = await app.service(builder.model)._get(file.owner_id, ctx, "update")
                if file.field not in builder.writable(ctx, "update", obj=obj):
                    raise PermissionDenied("File field is not writable.")
            files.rule(file.owner_type, file.field).check_name(body.filename)
            file.filename = body.filename
            return view(file)

    @router.get("/{fid}", name="obox_files_download")
    async def download(
        fid: UUID, request: Request, download: bool = False, who: Any = principal
    ) -> FileResponse:
        async with app.uow(principal=who, source="ui", request=request) as ctx:
            file = await ctx.session.get(File, fid)
            if file is None or file.status != "ready":
                raise NotFound("Unknown file.")
            await files.authorize(ctx, file)
        path = files.root / file.key
        if not await anyio.Path(path).is_file():
            raise NotFound("File is missing.")
        return FileResponse(
            path,
            filename=file.filename,
            media_type=file.content_type,
            content_disposition_type=(
                "inline" if not download and file.content_type in PREVIEW else "attachment"
            ),
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox",
                "Cache-Control": "private, no-store",
            },
        )

    return router
