"""The FastAPI adapter; public configuration belongs to conf.settings."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

from obox.apps import apps, validate_app_prefix
from obox.conf import settings

if TYPE_CHECKING:
    from obox.api import ModelApi


class Obox:
    def __init__(self, app: FastAPI) -> None:
        from obox.db.engine import close_engines
        from obox.runtime.config import RuntimeConfig
        from obox.runtime.registration import app_registrations
        from obox.runtime.runtime import _Runtime

        config = RuntimeConfig.from_settings()
        self.app = app
        self._runtime = runtime = _Runtime(
            app, settings=config, defer=True, enforce_permissions=True
        )
        app.state._obox_adapter = self
        apps.populate(settings.INSTALLED_APPS)
        entries = list(apps.get_app_configs())
        for entry in entries:
            ui_prefix = entry.ui_prefix if entry.ui_prefix is not None else entry.view_prefix
            validate_app_prefix(
                ui_prefix, "ui_prefix" if entry.ui_prefix is not None else "view_prefix"
            )
            runtime.add_app(entry)
        for entry in entries:
            factory = getattr(entry, "_service_factory", None)
            feature = factory() if factory is not None else None
            if feature is not None:
                runtime.activate(feature)
        runtime._loading_apps = False
        runtime.finish_apps()
        if settings.TEMPLATES:
            from importlib import import_module

            from fastapi.templating import Jinja2Templates

            definition = settings.TEMPLATES[0]
            module, _, name = definition["BACKEND"].rpartition(".")
            backend = getattr(import_module(module), name)(definition)
            runtime.templates = Jinja2Templates(env=backend.env)
        runtime.bind_templates()
        lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def managed(app: FastAPI) -> AsyncIterator[Any]:
            try:
                async with lifespan(app) as state:
                    yield state
            finally:
                await close_engines()

        app.router.lifespan_context = managed
        for entry in entries:
            ui_prefix = entry.ui_prefix if entry.ui_prefix is not None else entry.view_prefix
            for registration in [*app_registrations(entry), *entry._registrations]:
                registration.mount(
                    runtime, entry.label, view_prefix=ui_prefix, api_prefix=entry.api_prefix
                )

    def register(
        self,
        target: type[Any],
        *,
        slug: str | None = None,
        api: bool | type[ModelApi[Any]] = True,
        views: bool = True,
    ) -> None:
        from obox.runtime.registration import Registration

        Registration(target, slug, api, views).mount(self._runtime)
