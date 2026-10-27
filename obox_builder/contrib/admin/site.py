"""The admin site object: model registry, autodiscovery and router building."""

from __future__ import annotations

import logging
from importlib import import_module
from importlib.util import find_spec
from typing import TYPE_CHECKING, Any, TypeVar

from fastapi import APIRouter

from obox_builder.contrib.admin.navigation import NavGroup, NavItem, default_navigation
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.utils.strings import camel_to_snake

if TYPE_CHECKING:
    from obox_builder.contrib.admin.api.ratelimit import RateLimiter
    from obox_builder.contrib.admin.options import ModelAdmin

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=type[Any])


class AdminSite:
    """Central admin configuration: a registry of ModelAdmins plus navigation.

    Branding is configurable per site through ``site_header``, ``site_title``,
    and ``index_title`` constructor arguments or attributes.

    ``AdminConfig.ready()`` calls :meth:`autodiscover`, which imports each
    installed app's ``admin.py``. Registration is replace-on-conflict so test
    re-setups and module reloads are idempotent.
    """

    site_header: str = "obox admin"
    site_title: str = "obox admin"
    index_title: str = "Dashboard"
    api_auth_enabled: bool = False

    def __init__(
        self,
        name: str = "admin",
        *,
        import_path: str | None = None,
        site_header: str | None = None,
        site_title: str | None = None,
        index_title: str | None = None,
    ) -> None:
        self.name = name
        self.import_path = import_path
        self.site_header = site_header if site_header is not None else type(self).site_header
        if site_title is not None:
            self.site_title = site_title
        elif site_header is not None:
            self.site_title = site_header
        else:
            self.site_title = type(self).site_title
        self.index_title = index_title if index_title is not None else type(self).index_title
        self.navigation: list[NavGroup] = default_navigation()
        self._registry: dict[str, ModelAdmin] = {}
        self._discovered = False

    # -- registration -------------------------------------------------------
    def register(self, model: type[Any], admin_class: type[ModelAdmin] | None = None) -> Any:
        """Register ``model`` with an optional ModelAdmin class.

        Usable as ``@site.register(Model)``, ``@site.register(Model, Admin)``
        or ``site.register(Model, Admin)``.
        """

        def _register(cls: type[ModelAdmin]) -> type[ModelAdmin]:
            from obox_builder.contrib.admin.options import ModelAdmin as ModelAdminBase

            if not isinstance(cls, type) or not issubclass(cls, ModelAdminBase):
                raise ImproperlyConfigured(
                    f"{cls!r} is not a ModelAdmin subclass; cannot register {model.__name__}."
                )
            instance = cls(model, self)
            self._registry[instance.model_label] = instance
            return cls

        if admin_class is not None:
            return _register(admin_class)
        return _register

    def unregister(self, model: type[Any]) -> None:
        label = model_label_for(model)
        self._registry.pop(label, None)

    def get_registry(self) -> dict[str, ModelAdmin]:
        return dict(self._registry)

    def get_model_admin(self, app_label: str, model_name: str) -> ModelAdmin | None:
        return self._registry.get(f"{app_label}.{model_name}") or self._registry.get(
            f"{app_label}.{camel_to_snake(model_name)}"
        )

    def get_model_admin_for(self, model: type[Any]) -> ModelAdmin | None:
        return self._registry.get(model_label_for(model))

    # -- discovery ----------------------------------------------------------
    def autodiscover(self) -> None:
        """Import ``admin.py`` from every installed app (idempotent)."""
        if self._discovered:
            return
        self._discovered = True
        from obox_builder.apps import apps

        if not apps.apps_ready:
            return
        for config in apps.get_app_configs():
            module_path = f"{config.name}.admin"
            try:
                spec = find_spec(module_path)
            except (ImportError, ValueError):
                spec = None
            if spec is None:
                continue
            try:
                import_module(module_path)
            except Exception as exc:
                raise ImproperlyConfigured(
                    f"Error importing admin module for app {config.label!r}: {exc}"
                ) from exc

    # -- navigation ---------------------------------------------------------
    def get_nav_groups(self) -> list[NavGroup]:
        """Explicit navigation plus one group per app with registered admins."""
        from obox_builder.apps import apps

        groups = list(self.navigation)
        if apps.is_installed("obox_builder.contrib.jobs") and not any(
            item.url == "/admin/jobs/tasks" for group in groups for item in group.items
        ):
            groups.append(
                NavGroup(
                    label="Background jobs",
                    items=[
                        NavItem(
                            "Tasks",
                            "/admin/jobs/tasks",
                            icon="ti-list-check",
                            required_perm="jobs.view_task",
                        ),
                        NavItem(
                            "Workers",
                            "/admin/jobs/workers",
                            icon="ti-server",
                            required_perm="jobs.view_worker",
                        ),
                    ],
                )
            )
        registry_items: dict[str, list[NavItem]] = {}
        for admin in self._registry.values():
            group_label = self._app_label_text(admin.app_label)
            registry_items.setdefault(group_label, []).append(
                NavItem(
                    label=admin.plural_name,
                    url=admin.list_path(),
                    icon="ti-table",
                    required_perm=admin.permission_name("view"),
                    alternative_perms=(admin.permission_name("change"),),
                )
            )
        for group_label, items in registry_items.items():
            items.sort(key=lambda item: item.label)
            groups.append(NavGroup(label=group_label, items=items))
        return groups

    def _app_label_text(self, app_label: str) -> str:
        from obox_builder.apps import apps

        try:
            return apps.get_app_config(app_label).verbose_name
        except LookupError:
            return app_label.replace("_", " ").title()

    # -- router -------------------------------------------------------------
    def get_router(self) -> APIRouter:
        from obox_builder.contrib.admin.urls import build_router

        self.autodiscover()
        return build_router(self)

    def get_api_router(self, *, rate_limiter: RateLimiter | None = None) -> APIRouter:
        from obox_builder.contrib.admin.api.router import build_api_router

        self.autodiscover()
        return build_api_router(self, rate_limiter=rate_limiter)

    # -- tests --------------------------------------------------------------
    def _reset(self) -> None:
        self._registry.clear()
        self._discovered = False
        self.navigation = default_navigation()


def model_label_for(model: type[Any]) -> str:
    app_label = getattr(model, "_model_label", "") or getattr(model, "app_label", "")
    return f"{app_label}.{camel_to_snake(model.__name__)}"


site = AdminSite(import_path="obox_builder.contrib.admin.site.site")
