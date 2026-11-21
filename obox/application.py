"""The FastAPI adapter; public configuration belongs to conf.settings."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

from obox.apps import apps
from obox.conf import settings

if TYPE_CHECKING:
    from obox.builder.options import ModelBuilder


class Obox:
    def __init__(self, app: FastAPI) -> None:
        from obox._catalog import CatalogConfig
        from obox._config import RuntimeConfig
        from obox._runtime import _Runtime
        from obox.db import _close

        config = RuntimeConfig.from_settings()
        self.app = app
        self._runtime = runtime = _Runtime(
            app, settings=config, defer=True, enforce_permissions=True
        )
        app.state._obox_adapter = self
        apps.populate(settings.INSTALLED_APPS)
        for entry in apps.get_app_configs():
            catalog = CatalogConfig()
            catalog.name, catalog.label, catalog.verbose_name = (
                entry.name,
                entry.label,
                entry.verbose_name,
            )
            catalog.components = tuple(getattr(entry, "components", ()))
            catalog.pages = tuple(getattr(entry, "pages", ()))
            catalog.ui_prefix = getattr(entry, "ui_prefix", None)
            catalog.routers = tuple(getattr(entry, "routers", ()))
            runtime.add_app(catalog)
        for entry in apps.get_app_configs():
            factory = getattr(entry, "_service_factory", None)
            feature = factory() if factory is not None else None
            if feature is not None:
                runtime._activate(feature)
        runtime._loading_apps = False
        runtime._finish_apps()
        if settings.TEMPLATES:
            from importlib import import_module

            from fastapi.templating import Jinja2Templates

            definition = settings.TEMPLATES[0]
            module, _, name = definition["BACKEND"].rpartition(".")
            backend = getattr(import_module(module), name)(definition)
            runtime.templates = Jinja2Templates(env=backend.env)
        lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def managed(app: FastAPI) -> AsyncIterator[Any]:
            try:
                async with lifespan(app) as state:
                    yield state
            finally:
                await _close()

        app.router.lifespan_context = managed
        for entry in apps.get_app_configs():
            for registration in entry._registrations:
                registration.mount(runtime, entry.label)

    def register(
        self,
        target: type[Any],
        *,
        builder: type[ModelBuilder] | None = None,
        api: bool = True,
        views: bool = True,
    ) -> None:
        from obox.builder.registration import Registration

        Registration(target, builder, api, views).mount(self._runtime)
