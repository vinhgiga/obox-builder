"""Batched, authorized auxiliary metadata without changing record projections."""

from typing import Any, Literal

import sqlalchemy as sa
from pydantic import Field, StrictStr

from obox_builder.builder.api_schema import ApiRequest, ApiResponse
from obox_builder.builder.fields import schema, validate
from obox_builder.builder.query import invalid
from obox_builder.context import Context
from obox_builder.contrib.files.schemas import FileMetadata
from obox_builder.errors import NotFound, PermissionDenied


class MetadataQuery(ApiRequest):
    record_ids: list[StrictStr] = Field(min_length=1)
    include: list[Literal["attachments", "commentCount"]] = Field(min_length=1)


class AttachmentMetadata(FileMetadata):
    download_url: str


class RecordMetadata(ApiResponse):
    id: str
    attachments: dict[str, list[AttachmentMetadata]] | None = None
    comment_count: int | None = None


class MetadataResults(ApiResponse):
    items: list[RecordMetadata]


async def query_metadata(app: Any, builder: Any, data: MetadataQuery, ctx: Context) -> Any:
    from obox_builder.contrib.activity.models import Comment
    from obox_builder.contrib.activity.services import ownership
    from obox_builder.contrib.files.models import File

    service = app.service(builder.model)
    await service.authorize("retrieve", ctx)
    if "retrieve" not in builder.api.operations:
        raise NotFound("Metadata is unavailable.")
    if builder.api.max_batch is not None and len(data.record_ids) > builder.api.max_batch:
        raise invalid("record_ids", "Metadata query exceeds the configured batch maximum.")
    if len(set(data.include)) != len(data.include):
        raise invalid("include", "Choose unique metadata types.")
    files, activity = app.get_plugin("files"), app.get_plugin("activity")
    if ("attachments" in data.include and files is None) or (
        "commentCount" in data.include and activity is None
    ):
        raise invalid("include", "The requested metadata plugin is unavailable.")
    identity = schema("MetadataIdentity", builder.specs, (builder.pk_name,))
    keys = [validate(identity, {builder.pk_name: key})[builder.pk_name] for key in data.record_ids]
    if len(set(keys)) != len(keys):
        raise invalid("record_ids", "Choose unique record IDs.")
    if "commentCount" in data.include:
        await service.authorize("comment", ctx)
    results: list[dict[str, Any]] = []
    for start in range(0, len(keys), 500):
        batch = keys[start : start + 500]
        selected = (
            (
                await ctx.session.execute(
                    service._statement("retrieve", ctx)
                    .add_columns(sa.cast(builder.pk, sa.String).label("_obox_owner"))
                    .where(builder.pk.in_(batch))
                )
            )
            .unique()
            .all()
        )
        records = {getattr(obj, builder.pk_name): obj for obj, _ in selected}
        owners = {owner: str(getattr(obj, builder.pk_name)) for obj, owner in selected}
        if len(records) != len(batch):
            raise NotFound("A requested record is unavailable.")
        rows: dict[str, dict[str, Any]] = {}
        for key in batch:
            obj = records[key]
            await service.authorize("retrieve", ctx, obj)
            readable = builder.readable(ctx, obj=obj, fields=builder.api_read)
            if builder.pk_name not in readable:
                raise PermissionDenied("The primary key is not readable.")
            row: dict[str, Any] = {"id": str(key)}
            if "attachments" in data.include:
                row["attachments"] = {
                    builder.api_names[name]: []
                    for name in readable
                    if "obox_files" in builder.specs[name].column.info
                }
            if "commentCount" in data.include:
                await service.authorize("comment", ctx, obj)
                row["comment_count"] = 0
            rows[str(key)] = row
        if "attachments" in data.include:
            for file in await ctx.session.scalars(
                sa.select(File)
                .where(
                    File.owner_type == builder.model.__table__.fullname,
                    File.owner_id.in_(owners),
                    File.status == "ready",
                )
                .order_by(File.position, File.id)
            ):
                alias = builder.api_names.get(file.field)
                attachments = rows[owners[file.owner_id]]["attachments"]
                if alias in attachments:
                    root = ctx.request.scope.get("root_path", "") if ctx.request else ""
                    attachments[alias].append(
                        {**files.metadata(file), "download_url": f"{root}{files.prefix}/{file.id}"}
                    )
        if "commentCount" in data.include:
            permitted = set(
                await ctx.session.scalars(
                    service._statement("comment", ctx)
                    .with_only_columns(builder.pk)
                    .where(builder.pk.in_(batch))
                )
            )
            if permitted != set(batch):
                raise PermissionDenied("Comments are unavailable for a requested record.")
            owner = await ownership(activity, builder, records[batch[0]], ctx)
            counts = await ctx.session.execute(
                sa.select(Comment.record_id, sa.func.count())
                .where(
                    Comment.partition_key == owner["partition_key"],
                    Comment.resource_key == owner["resource_key"],
                    Comment.record_id.in_(rows),
                    Comment.deleted_at.is_(None),
                )
                .group_by(Comment.record_id)
            )
            for key, count in counts:
                rows[key]["comment_count"] = count
        results.extend(rows.values())
    return {"items": results}
