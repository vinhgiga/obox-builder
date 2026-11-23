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

from obox.contrib._models import authorize as authorize_model
from obox.contrib._models import get_object, installed_models
from obox.contrib.files.fields import attachments
from obox.contrib.files.models import File
from obox.contrib.files.rules import IMAGES, VIDEOS, Rule
from obox.contrib.files.schemas import FileMetadata
from obox.core.exceptions import (
    BuilderValidationError,
    ImproperlyConfigured,
    NotFound,
    PermissionDenied,
)

from . import claim as _claim  # noqa: F401 - register mapper listeners

if TYPE_CHECKING:
    from obox.runtime.context import Context
    from obox.runtime.runtime import _Runtime

__all__ = ["IMAGES", "VIDEOS", "File", "Files", "attachments"]
logger = logging.getLogger(__name__)


class Files:
    name = "files"
    requires: tuple[str, ...] = ()

    def __init__(
        self, *, root: str | Path, prefix: str = "/files", models: tuple[type[Any], ...] = ()
    ) -> None:
        self.models = models
        self.root = Path(root).resolve()
        self.prefix = prefix.rstrip("/")
        if not self.prefix.startswith("/") or any(c in self.prefix for c in "{}?#"):
            raise ImproperlyConfigured("Files.prefix must be an absolute URL path.")

    def _attach(self, app: _Runtime) -> None:
        from obox.contrib.files.routes import build_router

        if hasattr(self, "app"):
            raise ImproperlyConfigured("Files is already installed.")
        self.app = app
        self.models = self.models or installed_models()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        app.mount_static(
            self.prefix + "/_static",
            namespace="files",
            name="obox_files_static",
        )
        app.include_router(build_router(self), ui=True, permission="files.access_files")
        app.add_ui_prefix(self.prefix)

    @asynccontextmanager
    async def _lifespan(self, app: _Runtime) -> AsyncIterator[None]:
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

    def model(self, owner_type: str) -> type[Any]:
        for model in self.models:
            if model.__table__.fullname == owner_type:
                return model
        raise NotFound("Unknown file field.")

    def rule(self, owner_type: str, field: str) -> Rule:
        relation = sa.inspect(self.model(owner_type)).relationships.get(field)
        rule = relation.info.get("obox_files") if relation is not None else None
        if not isinstance(rule, Rule):
            raise NotFound("Unknown file field.")
        return rule

    def get_queryset(self, model: type[Any], ctx: Context) -> Any:
        return sa.select(model)

    def get_fields(
        self, model: type[Any], ctx: Context, obj: Any = None, *, write: bool = False
    ) -> tuple[str, ...]:
        return tuple(
            relation.key
            for relation in sa.inspect(model).relationships
            if "obox_files" in relation.info
        )

    async def owner(
        self,
        owner_type: str,
        identity: str | None,
        field: str,
        ctx: Context,
        *,
        write: bool = False,
    ) -> Any:
        model = self.model(owner_type)
        authorize_model(
            model, ctx.principal, "change" if write and identity else "add" if write else "view"
        )
        obj = (
            await get_object(model, identity, ctx, self.get_queryset(model, ctx), lock=write)
            if identity
            else None
        )
        if field not in self.get_fields(model, ctx, obj, write=write):
            raise PermissionDenied("This file field is not available.")
        return obj

    def form_config(self, model: type[Any], field: str, obj: Any, ctx: Context) -> dict[str, Any]:
        rule = self.rule(model.__table__.fullname, field)
        mapper = sa.inspect(model)
        pk_name = mapper.get_property_by_column(mapper.primary_key[0]).key
        return {
            "field": f"{model.__table__.fullname}.{field}",
            "owner_id": str(getattr(obj, pk_name)) if obj is not None else "",
            "accept": ",".join(rule.types or {}),
            "max_count": rule.max_count,
            "files": [self.metadata(file) for file in getattr(obj, field)]
            if obj is not None
            else [],
            "url": str(ctx.request.url_for("obox_files_create")) if ctx.request else self.prefix,
        }

    async def resolve(
        self, model: type[Any], values: dict[str, Any], ctx: Context, obj: Any = None
    ) -> dict[str, Any]:
        mapper = sa.inspect(model)
        for field in values.keys() & mapper.relationships.keys():
            if "obox_files" not in mapper.relationships[field].info:
                continue
            rule = self.rule(model.__table__.fullname, field)
            raw = values[field]
            try:
                if not isinstance(raw, list):
                    raise ValueError
                ids = [UUID(str(fid)) for fid in raw]
                if len(ids) != len(set(ids)):
                    raise ValueError
            except (ValueError, TypeError, AttributeError) as exc:
                raise BuilderValidationError({field: ["Use a list of unique file IDs."]}) from exc
            rule.count(field, len(ids))
            pk_name = mapper.get_property_by_column(mapper.primary_key[0]).key
            owner = str(getattr(obj, pk_name)) if obj is not None else None
            await self.owner(model.__table__.fullname, owner, field, ctx, write=True)
            query = sa.select(File).where(
                File.id.in_(ids),
                File.owner_type == model.__table__.fullname,
                File.field == field,
                File.status == "ready",
                sa.or_(
                    sa.and_(File.owner_id.is_(None), File.created_by == str(ctx.principal.id)),
                    File.owner_id == owner if owner is not None else sa.false(),
                ),
            )
            found = {file.id: file for file in await ctx.session.scalars(query)}
            if len(found) != len(ids):
                raise BuilderValidationError({field: ["File unavailable."]})
            values[field] = [found[fid] for fid in ids]
        return values

    @staticmethod
    def metadata(file: File) -> dict[str, Any]:
        return FileMetadata.model_validate(file).model_dump(mode="json")

    async def authorize(self, ctx: Context, file: File) -> None:
        if file.owner_id is None:
            from obox.contrib._models import permission

            model = self.model(file.owner_type)
            if (
                not getattr(ctx.principal, "is_authenticated", False)
                or not getattr(ctx.principal, "is_active", False)
                or file.created_by != str(getattr(ctx.principal, "id", None))
                or not any(permission(model, ctx.principal, action) for action in ("add", "change"))
                or file.field not in self.get_fields(model, ctx, write=True)
            ):
                raise NotFound("Unknown file.")
            return
        await self.owner(file.owner_type, file.owner_id, file.field, ctx)

    async def collect(self, *, max_age: timedelta = timedelta(hours=24)) -> int:
        """Remove expired attachments; also called by the hourly cleanup task."""
        cutoff = datetime.now(UTC) - max_age
        async with self.app.uow(principal=None) as ctx:
            for model in self.models:
                if not any(
                    "obox_files" in relation.info for relation in sa.inspect(model).relationships
                ):
                    continue
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
