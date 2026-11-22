"""The FastAPI adapter; public configuration belongs to conf.settings."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

from obox.apps import apps
from obox.conf import settings

if TYPE_CHECKING:
    from obox.api import ModelApi
    from obox.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView


class Obox:
    def __init__(self, app: FastAPI) -> None:
        from obox._catalog import CatalogConfig, validate_app_prefix
        from obox._config import RuntimeConfig
        from obox._registration import app_registrations
        from obox._runtime import _Runtime
        from obox.db import _close

        config = RuntimeConfig.from_settings()
        self.app = app
        self._runtime = runtime = _Runtime(
            app, settings=config, defer=True, enforce_permissions=True
        )
        app.state._obox_adapter = self
        apps.populate(settings.INSTALLED_APPS)
        entries = list(apps.get_app_configs())
        for entry in entries:
            validate_app_prefix(entry.view_prefix, "view_prefix")
            catalog = CatalogConfig()
            catalog.name, catalog.label, catalog.verbose_name = (
                entry.name,
                entry.label,
                entry.verbose_name,
            )
            catalog.components = tuple(getattr(entry, "components", ()))
            catalog.pages = tuple(getattr(entry, "pages", ()))
            catalog.ui_prefix = getattr(entry, "ui_prefix", None)
            catalog.api_prefix = entry.api_prefix
            catalog.routers = tuple(entry.routers)
            runtime.add_app(catalog)
        for entry in entries:
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
        for entry in entries:
            for registration in [*app_registrations(entry), *entry._registrations]:
                registration.mount(
                    runtime, entry.label, view_prefix=entry.view_prefix, api_prefix=entry.api_prefix
                )

    def register(
        self,
        target: type[Any],
        *,
        slug: str | None = None,
        api: bool | type[ModelApi[Any]] = True,
        views: bool = True,
        list_view: type[ListView] | None = None,
        detail_view: type[DetailView] | None = None,
        create_view: type[CreateView] | None = None,
        update_view: type[UpdateView] | None = None,
        delete_view: type[DeleteView] | None = None,
    ) -> None:
        from obox._registration import Registration

        Registration(
            target, slug, api, views, list_view, detail_view, create_view, update_view, delete_view
        ).mount(self._runtime)
