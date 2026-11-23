"""Authorized row history and author-owned comments."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa

from obox.components.query import invalid
from obox.contrib.activity.models import Comment, Event
from obox.contrib.activity.schemas import CommentRead
from obox.core.async_utils import invoke
from obox.core.exceptions import NotFound, PermissionDenied
from obox.runtime.context import Context
from obox.web.conditional import check_match

if TYPE_CHECKING:
    from obox.contrib.activity.apps import Activity


async def identity(activity: Activity, ids: set[str], ctx: Context) -> dict[Any, Any]:
    values = await invoke(activity.identities, ids, ctx) if activity.identities else {}
    return {
        key: values.get(key, {"id": None, "display_name": "System" if key is None else "Unknown"})
        for key in (None, *ids)
    }


def own(comment: Comment, ctx: Context) -> bool:
    return bool(
        getattr(ctx.principal, "is_authenticated", False)
        and getattr(ctx.principal, "is_active", True)
        and str(getattr(ctx.principal, "id", None)) == comment.author_id
    )


async def comment_row(activity: Activity, comment: Comment, ctx: Context) -> dict[str, Any]:
    actors = await identity(activity, {comment.author_id}, ctx)
    return {
        "id": str(comment.id),
        "author": actors[comment.author_id],
        "body": comment.body,
        "created_at": comment.created_at,
        "edited_at": comment.edited_at,
        "permissions": {"update": own(comment, ctx), "delete": own(comment, ctx)},
    }


def comment_etag(comment: Comment, row: dict[str, Any]) -> str:
    data = CommentRead.model_validate(row).model_dump_json()
    return '"' + hashlib.sha256(f"{comment.id}:{comment.version}:{data}".encode()).hexdigest() + '"'


def cursor(secret: bytes, scope: str, token: str | None = None, *, last: int | None = None) -> Any:
    key = hmac.digest(secret, b"obox.activity.cursor", "sha256")
    if last is not None:
        payload = base64.urlsafe_b64encode(json.dumps([scope, last]).encode()).rstrip(b"=")
        return (payload + b"." + hmac.digest(key, payload, "sha256").hex().encode()).decode()
    try:
        payload, signature = (token or "").encode("ascii").split(b".")
        if not hmac.compare_digest(signature, hmac.digest(key, payload, "sha256").hex().encode()):
            raise ValueError
        bound, value = json.loads(
            base64.b64decode(payload + b"=" * (-len(payload) % 4), altchars=b"-_", validate=True)
        )
        if bound != scope or type(value) is not int or not 0 < value < 2**63:
            raise ValueError
        return value
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise invalid("cursor", "Invalid cursor or changed query.", "invalid_cursor") from exc


async def comment(
    activity: Activity,
    model: Any,
    id: str,
    ctx: Context,
    *,
    comment_id: str | None = None,
    action: str = "get",
    body: str | None = None,
    conditions: list[str] | None = None,
    single: bool = False,
) -> Comment:
    await activity.get_object(
        model, id, ctx, action="view" if action == "get" else "comment", lock=action != "get"
    )
    owner = {"resource_key": model.__table__.fullname, "record_id": id}
    if action == "create":
        obj = Comment(**owner, author_id=str(ctx.principal.id), body=body)
        ctx.session.add(obj)
        await ctx.session.flush()
        ctx.session.add(
            Event(
                **owner,
                kind="comment",
                comment_id=obj.id,
                actor_id=obj.author_id,
                source=ctx.source,
            )
        )
    else:
        try:
            key = UUID(comment_id or "")
        except ValueError as exc:
            raise NotFound("Comment is unavailable.") from exc
        query = sa.select(Comment).filter_by(**owner, id=key, deleted_at=None)
        if action != "get":
            query = query.with_for_update().execution_options(populate_existing=True)
        loaded = await ctx.session.scalar(query)
        if loaded is None:
            raise NotFound("Comment is unavailable.")
        obj = loaded
        if action != "get":
            if not own(obj, ctx):
                raise PermissionDenied("Only the author may change this comment.")
            check_match(
                conditions or [],
                comment_etag(obj, await comment_row(activity, obj, ctx)),
                single=single,
            )
            if action == "update":
                obj.body, obj.edited_at = body, datetime.now(UTC)
            else:
                obj.body, obj.deleted_at = None, datetime.now(UTC)
    await ctx.session.flush()
    return obj


async def redact(activity: Activity, model: Any, changes: Any, ctx: Context) -> None:
    from obox.contrib._models import installed_models, permission

    tables = {other.__table__.fullname: other for other in installed_models()}
    mapper = sa.inspect(model)
    for change in changes:
        name = change["field"]
        if name not in mapper.column_attrs:
            # Detached attachments must not become an alternate download index.
            for side in ("before", "after"):
                if change.get(side):
                    change[side] = [None for _ in change[side]]
            continue
        for key in mapper.column_attrs[name].columns[0].foreign_keys:
            related = tables.get(key.column.table.fullname)
            for side in ("before", "after"):
                value = change.get(side)
                if value is None:
                    continue
                if related is None or not permission(related, ctx.principal, "view"):
                    change[side] = None
                    continue
                query = activity.get_queryset(related, ctx).where(key.column == value)
                if await ctx.session.scalar(query) is None:
                    change[side] = None


async def read(
    activity: Activity,
    model: Any,
    id: str,
    params: dict[str, str],
    ctx: Context,
    *,
    fields: Any = None,
) -> dict[str, Any]:
    obj = await activity.get_object(model, id, ctx)
    kind = params.get("kind", "all")
    if kind not in {"all", "revision", "comment"}:
        raise invalid("kind", "Use all, revision, or comment.")
    try:
        limit = int(params.get("limit", "50"))
        if not 1 <= limit <= 200:
            raise ValueError
    except ValueError as exc:
        raise invalid("limit", "Use a limit from 1 to 200.") from exc
    allowed = set(activity.get_fields(model, ctx, obj)).intersection(activity.fields[model])
    if fields is not None:
        allowed.intersection_update(fields)
    label = activity.label(model)
    scope = hashlib.sha256(
        json.dumps([label, id, str(ctx.principal.id), kind, sorted(allowed)]).encode()
    ).hexdigest()
    last = cursor(activity.app.csrf.secret, scope, params["cursor"]) if "cursor" in params else None
    stmt = (
        sa.select(Event, Comment)
        .outerjoin(
            Comment,
            sa.and_(
                Comment.id == Event.comment_id,
                Comment.resource_key == Event.resource_key,
                Comment.record_id == Event.record_id,
                Comment.deleted_at.is_(None),
            ),
        )
        .where(
            Event.resource_key == model.__table__.fullname,
            Event.record_id == id,
            sa.or_(Event.kind == "revision", Comment.id.is_not(None)),
        )
        .order_by(Event.id.desc())
    )
    if kind != "all":
        stmt = stmt.where(Event.kind == kind)
    items: list[dict[str, Any]] = []
    while len(items) <= limit:
        batch = list(
            await ctx.session.execute((stmt.where(Event.id < last) if last else stmt).limit(100))
        )
        if not batch:
            break
        for event, entry in batch:
            changes = [
                {
                    key: value
                    for key, value in change.items()
                    if key in {"field", "kind", "before", "after"}
                }
                for change in event.changes or ()
                if change.get("field") in allowed
            ]
            if event.kind == "revision" and not changes:
                continue
            await redact(activity, model, changes, ctx)
            actors = await identity(activity, {event.actor_id} if event.actor_id else set(), ctx)
            item = {
                "id": str(event.id),
                "kind": event.kind,
                "actor": actors[event.actor_id],
                "source": event.source,
                "occurred_at": event.occurred_at,
            }
            if entry is None:
                item.update(action=event.action, changes=changes)
            else:
                item["comment"] = await comment_row(activity, entry, ctx)
            items.append(item)
            if len(items) > limit:
                break
        last = batch[-1][0].id
    more = len(items) > limit
    items = items[:limit]
    return {
        "items": items,
        "page": {
            "limit": limit,
            "offset": 0,
            "has_more": more,
            "next_cursor": cursor(activity.app.csrf.secret, scope, last=int(items[-1]["id"]))
            if more
            else None,
            "total": None,
        },
    }
