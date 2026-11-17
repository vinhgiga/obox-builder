"""Transactional model history independent of CRUD routes and builders."""

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import sqlalchemy as sa

from obox._context import Context
from obox._exceptions import NotFound
from obox.apps import apps
from obox.contrib._models import authorize, get_object, installed_models
from obox.core.exceptions import ImproperlyConfigured


class Activity:
    name = "activity"
    requires: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        models: Mapping[Any, Sequence[str]] | None = None,
        identities: Callable[[set[str], Context], Any] | None = None,
    ) -> None:
        self.identities = identities
        self.fields: dict[type[Any], tuple[str, ...]] = {}
        for model, fields in (models or {}).items():
            self.track(model, fields=fields)

    def track(self, model: Any, *, fields: Sequence[str]) -> None:
        if isinstance(model, str):
            model = apps.get_model(model)
        mapper = sa.inspect(model)
        if (
            len(mapper.primary_key) != 1
            or not fields
            or any(
                name not in mapper.column_attrs
                and not (
                    name in mapper.relationships and "obox_files" in mapper.relationships[name].info
                )
                for name in fields
            )
        ):
            raise ImproperlyConfigured("History needs one primary key and explicit tracked fields.")
        self.fields[model] = tuple(fields)

    def model(self, label: str) -> type[Any]:
        for model in self.fields:
            if self.label(model) == label:
                return model
        raise NotFound("History is unavailable.")

    @staticmethod
    def label(model: type[Any]) -> str:
        from obox.views.generic.detail import _model_label

        return ".".join(_model_label(model))

    def get_queryset(self, model: type[Any], ctx: Context) -> Any:
        return sa.select(model)

    def get_fields(self, model: type[Any], ctx: Context, obj: Any = None) -> tuple[str, ...]:
        return self.fields.get(model, ())

    async def get_object(
        self,
        model: type[Any],
        identity: str,
        ctx: Context,
        *,
        action: str = "view",
        lock: bool = False,
    ) -> Any:
        authorize(model, ctx.principal, action)
        return await get_object(model, identity, ctx, self.get_queryset(model, ctx), lock=lock)

    @asynccontextmanager
    async def context(
        self, session: Any, *, actor: Any = None, source: str = "job"
    ) -> AsyncIterator[None]:
        """Include history in the caller's SQLAlchemy transaction."""
        target = getattr(session, "sync_session", session)
        previous = target.info.get("obox.history")
        target.info["obox.history"] = (self, actor, source)
        try:
            yield
            await session.flush()
        finally:
            if previous is None:
                target.info.pop("obox.history", None)
            else:
                target.info["obox.history"] = previous

    def _attach(self, app: Any) -> None:
        from obox.contrib.activity.routes import build_router

        if hasattr(self, "app"):
            raise ImproperlyConfigured("Activity is already installed.")
        self.app = app
        for model in installed_models():
            fields = getattr(model, "__history_fields__", ())
            if fields and model not in self.fields:
                self.track(model, fields=fields)
        app.include_router(build_router(self))


from obox.contrib.activity import tracking as _tracking  # noqa: E402, F401
