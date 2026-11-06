"""Scoped activity and comment operations inside the owning unit of work."""

import base64
import binascii
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import sqlalchemy as sa
from pydantic import TypeAdapter

from obox_builder.builder.conditional import check_match
from obox_builder.builder.fields import schema, validate
from obox_builder.builder.filters import FilterGroup
from obox_builder.builder.query import ListQuery, invalid
from obox_builder.builder.service import invoke
from obox_builder.context import Context
from obox_builder.contrib.activity import Activity
from obox_builder.contrib.activity.models import Comment, Event
from obox_builder.contrib.activity.schemas import CommentRead
from obox_builder.errors import (
    BuilderValidationError,
    ImproperlyConfigured,
    NotAuthenticated,
    NotFound,
    PermissionDenied,
)


async def ownership(activity: Activity, builder: Any, obj: Any, ctx: Context) -> dict[str, str]:
    partition = await invoke(activity.partition, ctx) if activity.partition else "default"
    if activity.partition is None and ctx.tenant is not None:
        raise ImproperlyConfigured("Tenant activity requires an explicit partition resolver.")
    if not isinstance(partition, str) or not partition or len(partition) > 255:
        raise ImproperlyConfigured(
            "Activity partition must be a nonempty string of at most 255 characters."
        )
    return {
        "partition_key": partition,
        "resource_key": builder.model.__table__.fullname,
        "record_id": str(getattr(obj, builder.pk_name)),
    }


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


async def comment(
    activity: Activity,
    builder: Any,
    id: str,
    ctx: Context,
    *,
    comment_id: str | None = None,
    action: str = "get",
    body: str | None = None,
    conditions: list[str] | None = None,
    single: bool = False,
) -> Comment:
    service = activity.app.service(builder.model)
    async with service._mutation(ctx, conditional=action in {"update", "delete"}):
        parent = await service.get(id, ctx=ctx)
        await service._get(id, ctx, "comment", lock=action != "get")
        owner = await ownership(activity, builder, parent, ctx)
        if action == "create":
            if not getattr(ctx.principal, "is_authenticated", False) or not getattr(
                ctx.principal, "is_active", True
            ):
                raise NotAuthenticated("An active authenticated author is required.")
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
            stmt = sa.select(Comment).filter_by(**owner, id=key, deleted_at=None)
            if action != "get":
                stmt = stmt.with_for_update().execution_options(populate_existing=True)
            loaded = await ctx.session.scalar(stmt)
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
                if action == "delete":
                    obj.body, obj.deleted_at = None, datetime.now(UTC)
                elif body != obj.body:
                    obj.body, obj.edited_at = body, datetime.now(UTC)
        await ctx.session.flush()
        return obj


async def snapshot(builder: Any, obj: Any, ctx: Context) -> dict[str, Any]:
    names = [
        name
        for name in builder.fields
        if builder.specs[name].column is not builder.version_column and name != builder.pk_name
    ]
    unloaded = sa.inspect(obj).unloaded.intersection(names)
    if unloaded:
        await ctx.session.refresh(obj, attribute_names=sorted(unloaded))
    return cast(
        dict[str, Any],
        TypeAdapter[Any](Any).dump_python(
            {name: builder.specs[name].serialize(getattr(obj, name)) for name in names}, mode="json"
        ),
    )


async def record(
    activity: Activity,
    builder: Any,
    action: str,
    obj: Any,
    before: dict[str, Any] | None,
    ctx: Context,
) -> None:
    after = {} if action == "delete" else await snapshot(builder, obj, ctx)
    changes = []
    for name in {**(before or {}), **after}:
        if action == "update" and (before or {}).get(name) == after.get(name):
            continue
        spec = builder.specs[name]
        kind = (
            "files"
            if "obox_files" in spec.column.info
            else "reference"
            if spec.column.foreign_keys
            else "number"
            if isinstance(spec.column.type, (sa.Numeric, sa.Integer, sa.Float))
            else "text"
        )
        changes.append(
            dict(
                field=name,
                kind=kind,
                **({"before": before[name]} if before is not None and name in before else {}),
                **({"after": after[name]} if name in after else {}),
            )
        )
    if changes:
        ctx.session.add(
            Event(
                **await ownership(activity, builder, obj, ctx),
                kind="revision",
                action=action,
                changes=changes,
                actor_id=str(ctx.principal.id)
                if getattr(ctx.principal, "id", None) is not None
                else None,
                source=ctx.source,
            )
        )


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


async def redact(
    activity: Activity, builder: Any, obj: Any, changes: list[dict[str, Any]], ctx: Context
) -> None:
    for field in {change["field"] for change in changes}:
        spec = builder.specs[field]
        slots = [
            (change, side)
            for change in changes
            if change["field"] == field
            for side in ("before", "after")
            if side in change and change[side] is not None
        ]
        if isinstance(spec.column.type, sa.BigInteger):
            for change, side in slots:
                change[side] = str(change[side])
        if "obox_files" in spec.column.info:
            allowed: set[str] = set()
            if activity.app.get_plugin("files") is not None:
                from obox_builder.contrib.files.models import File

                owner = sa.cast(
                    sa.literal(getattr(obj, builder.pk_name), type_=builder.pk.type), sa.String
                )
                allowed = {
                    str(fid)
                    for fid in await ctx.session.scalars(
                        sa.select(File.id).where(
                            File.owner_type == builder.model.__table__.fullname,
                            File.owner_id == owner,
                            File.field == field,
                            File.status == "ready",
                        )
                    )
                }
            for change, side in slots:
                value = change[side]
                change[side] = (
                    [fid if isinstance(fid, str) and fid in allowed else None for fid in value]
                    if isinstance(value, list)
                    else None
                )
        for fk in spec.column.foreign_keys:
            target = activity.app._for_table(fk.column.table)
            parsed = []
            if target is not None and await invoke(target.policy.allow, "retrieve", ctx):
                remote = sa.inspect(target.model).get_property_by_column(fk.column).key
                if remote not in target.specs:
                    for change, side in slots:
                        change[side] = None
                    continue
                identity = schema("ActivityReference", target.specs, (remote,))
                for change, side in slots:
                    if change[side] is None:
                        continue
                    try:
                        value = validate(identity, {remote: change[side]})[remote]
                    except BuilderValidationError:
                        continue
                    parsed.append((change, side, value))
                service = activity.app.service(target.model)
                stmt = service._statement("retrieve", ctx).where(
                    getattr(target.model, remote).in_([value for _, _, value in parsed])
                )
                allowed_values = set()
                for related in (await ctx.session.scalars(stmt)).unique():
                    fields = target.readable(ctx, obj=related, fields=target.api_read)
                    if (
                        target.pk_name in fields
                        and remote in fields
                        and await invoke(target.policy.allow, "retrieve", ctx, obj=related)
                    ):
                        allowed_values.add(str(getattr(related, remote)))
                permitted = {
                    (id(change), side)
                    for change, side, value in parsed
                    if str(value) in allowed_values
                }
            else:
                permitted = set()
            for change, side in slots:
                if (id(change), side) not in permitted:
                    change[side] = None


async def read(
    activity: Activity,
    builder: Any,
    id: str,
    params: dict[str, str],
    ctx: Context,
    *,
    fields: Any = None,
) -> dict[str, Any]:
    app = activity.app
    service = app.service(builder.model)
    obj = await service.get(id, ctx=ctx)
    kind = params.get("kind", "all")
    if kind not in {"all", "revision", "comment"}:
        raise invalid("kind", "Use all, revision, or comment.")
    limit = ListQuery(params, FilterGroup()).integer("limit", builder.api.default_limit)
    if builder.api.max_limit is not None:
        if limit is not None and limit > builder.api.max_limit:
            raise invalid("limit", "Limit exceeds the configured maximum.")
        limit = builder.api.max_limit if limit is None else limit
    if "cursor" in params and not limit:
        raise invalid("cursor", "Cursor requires a positive limit.")
    key = getattr(obj, builder.pk_name)
    kinds = []
    for entry_kind, action in (("revision", "history"), ("comment", "comment")):
        if kind not in {"all", entry_kind}:
            continue
        if (
            await invoke(builder.policy.allow, action, ctx)
            and await invoke(builder.policy.allow, action, ctx, obj=obj)
            and await ctx.session.scalar(
                sa.select(
                    service._statement(action, ctx)
                    .where(getattr(builder.model, builder.pk_name) == key)
                    .exists()
                )
            )
        ):
            kinds.append(entry_kind)
    if not kinds:
        raise PermissionDenied("The requested activity is not permitted.")
    partition = (await ownership(activity, builder, obj, ctx))["partition_key"]
    resource, record_id = builder.model.__table__.fullname, str(key)
    fields = set(
        builder.readable(ctx, obj=obj, fields=builder.api_read if fields is None else fields)
    )
    binding = [
        partition,
        resource,
        record_id,
        str(getattr(ctx.principal, "id", None)),
        kind,
        kinds,
        limit,
        sorted(fields),
    ]
    scope = hashlib.sha256(json.dumps(binding).encode()).hexdigest()
    last = cursor(app.csrf.secret, scope, params["cursor"]) if "cursor" in params else None
    stmt = (
        sa.select(Event, Comment)
        .outerjoin(
            Comment,
            sa.and_(
                Comment.id == Event.comment_id,
                Comment.partition_key == Event.partition_key,
                Comment.resource_key == Event.resource_key,
                Comment.record_id == Event.record_id,
                Comment.deleted_at.is_(None),
            ),
        )
        .where(
            Event.partition_key == partition,
            Event.resource_key == resource,
            Event.record_id == record_id,
            Event.kind.in_(kinds),
            sa.or_(Event.kind == "revision", Comment.id.is_not(None)),
        )
        .order_by(Event.id.desc())
    )
    items: list[dict[str, Any]] = []
    wanted = None if limit is None else max(limit, 1) + (limit != 0)
    while wanted is None or len(items) < wanted:
        query = stmt.where(Event.id < last) if last is not None else stmt
        batch = list(await ctx.session.execute(query.limit(100)))
        if not batch:
            break
        projected = {
            event.id: [
                {
                    name: value
                    for name, value in change.items()
                    if name in {"field", "kind", "before", "after"}
                }
                for change in event.changes or ()
                if change.get("field") in fields
            ]
            for event, _ in batch
            if event.kind == "revision"
        }
        await redact(
            activity,
            builder,
            obj,
            [change for changes in projected.values() for change in changes],
            ctx,
        )
        ids = {
            value
            for event, comment in batch
            for value in (event.actor_id, comment.author_id if comment else None)
            if value is not None
        }
        identities = await invoke(activity.identities, ids, ctx) if activity.identities else {}

        actors = {
            value: identities.get(
                value, {"id": None, "display_name": "System" if value is None else "Unknown"}
            )
            for value in (None, *ids)
        }

        for event, comment in batch:
            if event.kind == "revision" and not projected[event.id]:
                continue
            item = {
                "id": str(event.id),
                "kind": event.kind,
                "actor": actors[event.actor_id],
                "source": event.source,
                "occurred_at": event.occurred_at,
            }
            if event.kind == "revision":
                item.update(action=event.action, changes=projected[event.id])
            else:
                own = (
                    getattr(ctx.principal, "is_authenticated", False)
                    and getattr(ctx.principal, "is_active", True)
                    and str(getattr(ctx.principal, "id", None)) == comment.author_id
                )
                item["comment"] = {
                    "id": str(comment.id),
                    "author": actors[comment.author_id],
                    "body": comment.body,
                    "created_at": comment.created_at,
                    "edited_at": comment.edited_at,
                    "permissions": {"update": own, "delete": own},
                }
            items.append(item)
            if wanted is not None and len(items) == wanted:
                break
        last = batch[-1][0].id
    more = limit is not None and len(items) > limit
    items = items if limit is None else items[:limit]
    return {
        "items": items,
        "page": {
            "limit": limit,
            "offset": 0,
            "has_more": more,
            "next_cursor": cursor(app.csrf.secret, scope, last=int(items[-1]["id"]))
            if more and items
            else None,
            "total": None,
        },
    }
