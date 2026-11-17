"""The FastAPI adapter; public configuration belongs to conf.settings."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from obox.apps import apps
from obox.conf import settings


class Obox:
    def __init__(self, app: FastAPI) -> None:
        from obox._catalog import CatalogConfig
        from obox._config import RuntimeConfig
        from obox._runtime import _Runtime
        from obox.db import _close

        config = RuntimeConfig.from_settings()
        self.app = app
        self._runtime = runtime = _Runtime(app, settings=config, defer=True)
        app.state._obox_adapter = self
        apps.populate(settings.INSTALLED_APPS)
        for entry in apps.get_app_configs():
            catalog = CatalogConfig()
            catalog.name, catalog.label, catalog.verbose_name = (
                entry.name,
                entry.label,
                entry.verbose_name,
            )
            for name in (
                "components",
                "pages",
                "routers",
                "ui_prefix",
                "tab_bar_color",
            ):
                if hasattr(entry, name):
                    setattr(catalog, name, getattr(entry, name))
            runtime.add_app(catalog)
        for entry in apps.get_app_configs():
            factory = getattr(entry, "_service_factory", None)
            feature = factory() if factory is not None else None
            if feature is not None:
                runtime._activate(feature)
        runtime._loading_apps = False
        for feature in runtime._pending_features:
            runtime._attach_feature(feature)
        runtime._pending_features.clear()
        runtime._product_protocol = apps.is_installed("obox.contrib.resources")
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
        if settings.ROOT_URLCONF:
            from importlib import import_module

            from obox.urls import _mount

            _mount(app, import_module(settings.ROOT_URLCONF).urlpatterns)
