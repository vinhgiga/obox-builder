"""Comment operations shared by Python, HTML and REST callers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field

from obox_builder.apps import apps
from obox_builder.contrib.comments import (
    COMMENTS_ADD_PERMISSION,
    COMMENTS_MODERATE_PERMISSION,
    COMMENTS_VIEW_PERMISSION,
)
from obox_builder.contrib.comments.exceptions import CommentConflict, CommentNotFound
from obox_builder.contrib.comments.models import RowComment
from obox_builder.contrib.comments.policy import CommentPolicy
from obox_builder.contrib.comments.queries import get_comment, thread_page
from obox_builder.contrib.comments.registry import get_options, is_commentable
from obox_builder.contrib.comments.service import CommentService
from obox_builder.contrib.comments.targets import CommentTarget
from obox_builder.exceptions import PermissionDenied
from obox_builder.resources import (
    OperationContext,
    ReadOnlyModelResource,
    ResourcePolicy,
    capability,
    get_registry,
    operation,
    register,
)
from obox_builder.security import PermissionRef


class AddComment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str
    parent_id: str | None = None
    idempotency_key: str | None = None


class CommentID(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment_id: str
    version: int = Field(ge=1)


class EditComment(CommentID):
    body: str


class ResolveComment(CommentID):
    resolved: bool


class CommentQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    before: int | None = Field(default=None, ge=1)
    limit: int = Field(default=20, ge=1, le=100)


class CommentData(BaseModel):
    id: str
    parent_id: str | None
    body: str | None
    author_id: str | None
    author_label: str
    created_at: datetime
    edited_at: datetime | None
    resolved_at: datetime | None
    deleted_at: datetime | None
    version: int


class CommentThread(BaseModel):
    root: CommentData
    replies: list[CommentData]


class CommentPage(BaseModel):
    items: list[CommentThread]
    count: int
    older: int | None


def comment_data(row: RowComment, policy: CommentPolicy) -> CommentData:
    return CommentData(
        id=str(row.id),
        parent_id=str(row.parent_id) if row.parent_id is not None else None,
        body=row.body if row.deleted_at is None or policy.can_read_deleted() else None,
        author_id=str(row.author_id) if row.author_id is not None else None,
        author_label=row.author_label,
        created_at=row.created_at,
        edited_at=row.edited_at,
        resolved_at=row.resolved_at,
        deleted_at=row.deleted_at,
        version=row.version,
    )


def comment_identity(value: str) -> int:
    try:
        pk = int(value)
    except ValueError as exc:
        raise CommentNotFound("Comment is unavailable.") from exc
    if not 0 < pk < 2**63:
        raise CommentNotFound("Comment is unavailable.")
    return pk


@dataclass(frozen=True)
class ResourceCommentPolicy(CommentPolicy):
    ctx: OperationContext
    resource: ReadOnlyModelResource[Any]

    def can_change_model(self) -> bool:
        return self.resource.can_execute(self.ctx, "update", self.ctx.obj)


class Comments(ReadOnlyModelResource[Any]):
    """Mix into a commentable model resource before ModelResource."""

    def __init__(self) -> None:
        super().__init__()
        if (
            not apps.apps_ready
            or not apps.is_installed("obox_builder.contrib.comments")
            or not is_commentable(self.model)
        ):
            self.exclude_operations = (
                *self.exclude_operations,
                "listComments",
                "addComment",
                "editComment",
                "deleteComment",
                "moderateComment",
                "resolveComment",
            )

    def comment_context(self, ctx: OperationContext, obj: Any):
        if not self.can_execute(ctx, "get", obj):
            raise PermissionDenied("The commented record is unavailable.")
        target = CommentTarget(self.scope_name, str(self.get_pk(obj)), self.record_label(ctx, obj))
        read = self.operations["get"].permission.name
        update = self.operations.get("update")
        policy = ResourceCommentPolicy(
            ctx.actor,
            read,
            update.permission.name if update is not None else f"{self.name}.update",
            ctx,
            self,
        )
        return target, policy, get_options(self.model)

    async def check_comment_version(
        self, ctx: OperationContext, target: CommentTarget, data: CommentID
    ) -> int:
        identity = comment_identity(data.comment_id)
        row = await get_comment(ctx.session, target, identity)
        if row is None:
            raise CommentNotFound("Comment is unavailable.")
        if row.version != data.version:
            raise CommentConflict("This comment changed; reload before trying again.")
        return identity

    @operation(
        name="listComments",
        scope="record",
        effect="read",
        permission=PermissionRef.parse(COMMENTS_VIEW_PERMISSION),
    )
    async def list_comments(
        self, ctx: OperationContext, obj: Any, data: CommentQuery
    ) -> CommentPage:
        target, policy, options = self.comment_context(ctx, obj)
        page = await thread_page(
            ctx.session, target, policy, options, before=data.before, limit=data.limit
        )
        ids = [comment.id for thread in page.threads for comment in (thread.root, *thread.replies)]
        rows = {
            row.id: row
            for row in await ctx.session.scalars(
                sa.select(RowComment).where(RowComment.id.in_(ids))
            )
        }
        return CommentPage(
            items=[
                CommentThread(
                    root=comment_data(rows[thread.root.id], policy),
                    replies=[comment_data(rows[reply.id], policy) for reply in thread.replies],
                )
                for thread in page.threads
            ],
            count=page.count,
            older=page.older,
        )

    @operation(
        name="addComment", scope="record", permission=PermissionRef.parse(COMMENTS_ADD_PERMISSION)
    )
    async def add_comment(self, ctx: OperationContext, obj: Any, data: AddComment) -> CommentData:
        target, policy, options = self.comment_context(ctx, obj)
        row = await CommentService(ctx.session, policy).add(
            target,
            options,
            data.model_dump(),
            source=str(ctx.metadata.get("source", "system")),
            request_id=ctx.metadata.get("request_id"),
        )
        return comment_data(row, policy)

    @operation(
        name="editComment", scope="record", permission=PermissionRef.parse(COMMENTS_ADD_PERMISSION)
    )
    async def edit_comment(self, ctx: OperationContext, obj: Any, data: EditComment) -> CommentData:
        target, policy, options = self.comment_context(ctx, obj)
        row = await CommentService(ctx.session, policy).edit(
            target,
            options,
            await self.check_comment_version(ctx, target, data),
            {"body": data.body},
        )
        return comment_data(row, policy)

    @operation(
        name="deleteComment",
        scope="record",
        effect="delete",
        permission=PermissionRef.parse(COMMENTS_ADD_PERMISSION),
    )
    async def delete_comment(self, ctx: OperationContext, obj: Any, data: CommentID) -> CommentData:
        target, policy, _options = self.comment_context(ctx, obj)
        row = await CommentService(ctx.session, policy).delete(
            target, await self.check_comment_version(ctx, target, data)
        )
        return comment_data(row, policy)

    @operation(
        name="moderateComment",
        scope="record",
        effect="delete",
        permission=PermissionRef.parse(COMMENTS_MODERATE_PERMISSION),
    )
    async def moderate_comment(
        self, ctx: OperationContext, obj: Any, data: CommentID
    ) -> CommentData:
        target, policy, _options = self.comment_context(ctx, obj)
        row = await CommentService(ctx.session, policy).delete(
            target, await self.check_comment_version(ctx, target, data)
        )
        return comment_data(row, policy)

    @operation(
        name="resolveComment",
        scope="record",
        permission=PermissionRef.parse(COMMENTS_ADD_PERMISSION),
    )
    async def resolve_comment(
        self, ctx: OperationContext, obj: Any, data: ResolveComment
    ) -> CommentData:
        target, policy, _options = self.comment_context(ctx, obj)
        row = await CommentService(ctx.session, policy).set_resolved(
            target, await self.check_comment_version(ctx, target, data), resolved=data.resolved
        )
        return comment_data(row, policy)


def unrestricted(resource: ReadOnlyModelResource) -> bool:
    return (
        type(resource.policy).scope is ResourcePolicy.scope
        and type(resource.policy).allows is ResourcePolicy.allows
        and type(resource).get_queryset is ReadOnlyModelResource.get_queryset
    )


class CommentRowsPolicy(ResourcePolicy):
    def scope(self, ctx, query, operation):
        registry = ctx.registry or get_registry()
        clauses = []
        for resource in registry.resources.values():
            if (
                not isinstance(resource, ReadOnlyModelResource)
                or resource.model is RowComment
                or "listComments" not in resource.operations
            ):
                continue
            context = ctx.for_resource(resource)
            if not resource.can_execute(context, "get"):
                continue
            pks = (
                resource.scoped_query(context, resource.operations["get"])
                .with_only_columns(sa.cast(resource.pk_column(), sa.String))
                .order_by(None)
            )
            visible = RowComment.object_pk.in_(pks)
            if unrestricted(resource):
                visible = sa.true()
            clauses.append(sa.and_(RowComment.model_label == resource.scope_name, visible))
        return query.where(sa.or_(*clauses) if clauses else sa.false())


@register()
class RowCommentResource(ReadOnlyModelResource[RowComment]):
    name = "comments.rowComment"
    label = "Comment"
    model = RowComment
    fields = (
        "id",
        "model_label",
        "object_pk",
        "object_repr",
        "parent_id",
        "body",
        "body_format",
        "author_id",
        "author_label",
        "source",
        "created_at",
        "edited_at",
        "resolved_at",
        "resolved_by_id",
        "resolved_by_label",
        "deleted_at",
        "deleted_by_id",
        "deleted_by_label",
        "version",
    )
    create = capability("Can create comments", effect="write")
    moderate = capability("Can moderate comments", effect="write")
    export = capability("Can export comments")
    ordering = ("-id",)

    policy = CommentRowsPolicy()
    fields_config = {
        "body": {
            "show_if": lambda ctx: (
                ctx.obj.deleted_at is None or ctx.user.has_perm(COMMENTS_MODERATE_PERMISSION)
            )
        }
    }

    async def record_visible(self, ctx, obj):
        registry = ctx.registry or get_registry()
        resource = next(
            (item for item in registry.resources.values() if item.scope_name == obj.model_label),
            None,
        )
        if not isinstance(resource, ReadOnlyModelResource):
            return False
        context = ctx.for_resource(resource)
        if not resource.can_execute(context, "get"):
            return False
        try:
            target = await resource.resolve_target(
                context, resource.operations["get"], obj.object_pk
            )
        except LookupError:
            return unrestricted(resource)
        return resource.can_execute(context, "get", target)

    async def resolve_target(self, ctx, spec, target):
        obj = await super().resolve_target(ctx, spec, target)
        if not await self.record_visible(ctx, obj):
            raise LookupError("Comment is unavailable.")
        return obj

    async def serialize(self, ctx, obj):
        data = (await super().serialize(ctx, obj)).model_dump(exclude_unset=True)
        data["object_repr"] = f"#{obj.object_pk}"
        return self.read_schema.model_validate(data)
