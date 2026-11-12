"""Local resumable uploads and model-declared attachment collections."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import anyio
import sqlalchemy as sa

from obox_builder.contrib.files import claim as _claim  # noqa: F401 - register mapper listeners
from obox_builder.contrib.files.fields import attachments
from obox_builder.contrib.files.models import File
from obox_builder.contrib.files.rules import IMAGES, VIDEOS, Rule
from obox_builder.contrib.files.schemas import FileMetadata
from obox_builder.errors import (
    BuilderValidationError,
    ImproperlyConfigured,
    NotFound,
    PermissionDenied,
)

if TYPE_CHECKING:
    from obox_builder.application import Obox
    from obox_builder.context import Context

__all__ = ["IMAGES", "VIDEOS", "File", "Files", "attachments"]
logger = logging.getLogger(__name__)


class Files:
    name = "files"
    requires: tuple[str, ...] = ()

    def __init__(self, *, root: str | Path, prefix: str = "/files") -> None:
        self.root = Path(root).resolve()
        self.prefix = prefix.rstrip("/")
        if not self.prefix.startswith("/") or any(c in self.prefix for c in "{}?#"):
            raise ImproperlyConfigured("Files.prefix must be an absolute URL path.")

    def setup(self, app: Obox) -> None:
        from obox_builder.contrib.files.routes import build_router

        if hasattr(self, "app"):
            raise ImproperlyConfigured("Files is already installed.")
        self.app = app
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        app.mount_static(
            self.prefix + "/_static",
            namespace="files",
            name="obox_files_static",
        )
        app.include_router(build_router(self), ui=True)
        app.add_ui_prefix(self.prefix)

    @asynccontextmanager
    async def lifespan(self, app: Obox) -> AsyncIterator[None]:
        async def cleanup() -> None:
            while True:
                await anyio.sleep(3600)
                try:
                    await self.collect()
                except Exception:
                    logger.exception("File cleanup failed.")

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(cleanup)
            try:
                yield
            finally:
                tasks.cancel_scope.cancel()

    def builder(self, owner_type: str) -> Any:
        for registration in self.app._registrations.values():
            if registration.model.__table__.fullname == owner_type:
                return registration.builder
        raise NotFound("Unknown file field.")

    def rule(self, owner_type: str, field: str) -> Rule:
        builder = self.builder(owner_type)
        spec = builder.specs.get(field)
        rule = spec.column.info.get("obox_files") if spec else None
        if not isinstance(rule, Rule) or field not in builder.specs:
            raise NotFound("Unknown file field.")
        return rule

    async def resolve(
        self, builder: Any, values: dict[str, Any], ctx: Context, obj: Any = None
    ) -> dict[str, Any]:
        for field, value in values.items():
            if "obox_files" not in builder.specs[field].column.info:
                continue
            rule = self.rule(builder.model.__table__.fullname, field)
            rule.count(field, len(value))
            try:
                ids = [UUID(fid) for fid in value]
            except ValueError as exc:
                raise BuilderValidationError({field: ["Use valid file IDs."]}) from exc
            if len(set(ids)) != len(ids):
                raise BuilderValidationError({field: ["Use unique file IDs."]})
            actor = getattr(ctx.principal, "id", None)
            owner = (
                sa.cast(sa.literal(getattr(obj, builder.pk_name), type_=builder.pk.type), sa.String)
                if obj is not None
                else None
            )
            records = {
                file.id: file
                for file in await ctx.session.scalars(
                    sa.select(File).where(
                        File.id.in_(ids),
                        File.status == "ready",
                        File.owner_type == builder.model.__table__.fullname,
                        File.field == field,
                        sa.or_(
                            File.owner_id == owner if owner is not None else sa.false(),
                            sa.and_(
                                File.owner_id.is_(None),
                                File.created_by == str(actor) if actor is not None else sa.false(),
                            ),
                        ),
                    )
                )
            }
            if len(records) != len(ids):
                raise BuilderValidationError({field: ["File unavailable."]})
            values[field] = [records[fid] for fid in ids]
        return values

    def form_config(self, builder: Any, field: str, obj: Any, ctx: Context) -> dict[str, Any]:
        rule = self.rule(builder.model.__table__.fullname, field)
        return {
            "field": f"{builder.model.__table__.fullname}.{field}",
            "owner_id": str(getattr(obj, builder.pk_name)) if obj is not None else "",
            "accept": ",".join(rule.types or {}),
            "max_count": rule.max_count,
            "files": [self.metadata(file) for file in getattr(obj, field)]
            if obj is not None
            else [],
            "url": str(ctx.request.url_for("obox_files_create")) if ctx.request else self.prefix,
        }

    @staticmethod
    def metadata(file: File) -> dict[str, Any]:
        return FileMetadata.model_validate(file).model_dump(mode="json")

    async def authorize(self, ctx: Context, file: File) -> None:
        if file.owner_id is None:
            if (
                not getattr(ctx.principal, "is_authenticated", False)
                or not getattr(ctx.principal, "is_active", True)
                or file.created_by != str(getattr(ctx.principal, "id", None))
            ):
                raise NotFound("Unknown file.")
            if getattr(ctx.principal, "api_scopes", None) is not None:
                builder = self.builder(file.owner_type)
                service = self.app.service(builder.model)
                for action in ("create", "update"):
                    if await service.permitted(action, ctx) and file.field in builder.writable(
                        ctx, action, surface="api"
                    ):
                        return
                raise NotFound("Unknown file.")
            return
        builder = self.builder(file.owner_type)
        obj = await self.app.service(builder.model).get(file.owner_id, ctx=ctx)
        fields = builder.api_read if ctx.source == "api" else builder.fields
        if file.field not in builder.readable(ctx, obj=obj, fields=fields):
            raise PermissionDenied("File field is not readable.")

    async def collect(self, *, max_age: timedelta = timedelta(hours=24)) -> int:
        """Remove expired attachments; also called by the hourly cleanup task."""
        cutoff = datetime.now(UTC) - max_age
        async with self.app.uow(principal=None) as ctx:
            for registration in self.app._registrations.values():
                if not any(
                    "obox_files" in spec.column.info for spec in registration.builder.specs.values()
                ):
                    continue
                model = registration.model
                pk = sa.inspect(model).primary_key[0]
                await ctx.session.execute(
                    sa.update(File)
                    .where(
                        File.owner_type == model.__table__.fullname,
                        File.owner_id.is_not(None),
                        ~sa.exists(sa.select(pk).where(sa.cast(pk, sa.String) == File.owner_id)),
                    )
                    .values(owner_id=None, unclaimed_at=datetime.now(UTC))
                    .execution_options(synchronize_session=False)
                )
            removed = list(
                (
                    await ctx.session.execute(
                        sa.delete(File)
                        .where(
                            File.owner_id.is_(None),
                            File.unclaimed_at < cutoff,
                        )
                        .returning(File.id, File.key)
                    )
                ).all()
            )
        for fid, key in removed:
            await anyio.Path(self.root / key).unlink(missing_ok=True)
            await anyio.Path(self.root / "tmp" / str(fid)).unlink(missing_ok=True)
        # A failed DB commit can leave a file without a metadata row.
        async with self.app.uow(principal=None) as ctx:
            live = set((await ctx.session.scalars(sa.select(File.id))).all())

        def sweep() -> None:
            for path in self.root.glob("*/*"):
                if not path.is_file() or path.stat().st_mtime >= cutoff.timestamp():
                    continue
                try:
                    fid = UUID(path.name.split(".", 1)[0])
                except ValueError:
                    continue
                if fid not in live or "." in path.name:
                    path.unlink(missing_ok=True)

        await anyio.to_thread.run_sync(sweep)
        return len(removed)
