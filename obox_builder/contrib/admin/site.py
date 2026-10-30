"""The admin site object: model registry, autodiscovery and router building."""

from __future__ import annotations

import logging
from importlib import import_module
from importlib.util import find_spec
from typing import TYPE_CHECKING, Any, TypeVar

from fastapi import APIRouter, Request

from obox_builder.conf import settings
from obox_builder.contrib.admin.navigation import NavGroup, NavItem, default_navigation
from obox_builder.exceptions import ImproperlyConfigured
from obox_builder.http.urls import is_safe_url
from obox_builder.utils.strings import camel_to_snake

if TYPE_CHECKING:
    from obox_builder.contrib.admin.options import ModelView
    from obox_builder.contrib.auth.models import User

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=type[Any])


class AdminSite:
    """Central admin configuration: a registry of ModelViews plus navigation.

    Branding is configurable per site through ``site_header``, ``site_title``,
    and ``index_title`` constructor arguments or attributes.

    ``AdminConfig.ready()`` calls :meth:`autodiscover`, which imports each
    installed app's ``admin.py``. Registration is replace-on-conflict so test
    re-setups and module reloads are idempotent.
    """

    site_header: str = "obox admin"
    site_title: str = "obox admin"
    index_title: str = "Dashboard"

    def __init__(
        self,
        name: str = "admin",
        *,
        import_path: str | None = None,
        site_header: str | None = None,
        site_title: str | None = None,
        index_title: str | None = None,
        registry: Any = None,
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
        self._registry: dict[str, ModelView] = {}
        self._discovered = False
        self._resource_registry = registry
        self._declarations: dict[str, tuple[type[Any], type[ModelView]]] = {}

    @property
    def resource_registry(self):
        from obox_builder.resources import get_registry

        return self._resource_registry if self._resource_registry is not None else get_registry()

    async def get_default_redirect_url(self, request: Request, user: User) -> str:
        """Return the landing page when login has no safe ``next`` target."""
        return settings.LOGIN_REDIRECT_URL

    async def get_success_url(self, request: Request, user: User) -> str:
        """Prefer a safe POST/GET ``next``, then the per-user default."""
        data = await request.form() if request.method == "POST" else request.query_params
        candidate = data.get("next", request.query_params.get("next", ""))
        if isinstance(candidate, str) and is_safe_url(candidate):
            return candidate
        return await self.get_default_redirect_url(request, user)

    # -- registration -------------------------------------------------------

    def register(self, resource, view_class=None):
        from obox_builder.contrib.admin.options import ModelView
        from obox_builder.resources import ReadOnlyModelResource

        definition = resource if isinstance(resource, type) else type(resource)
        if not issubclass(definition, ReadOnlyModelResource):
            raise ImproperlyConfigured("AdminSite.register expects a model Resource class.")
        view_class = view_class or ModelView
        if not issubclass(view_class, ModelView):
            raise ImproperlyConfigured("HTML adapters must extend ModelView.")
        instance = view_class(resource, self)
        previous = self._declarations.get(instance.resource.name)
        if previous is not None and previous != (definition, view_class):
            raise ImproperlyConfigured(
                f"Resource {instance.resource.name!r} already has a view binding."
            )
        self._declarations[instance.resource.name] = (definition, view_class)
        self._registry[instance.model_label] = instance
        return instance

    def view(self, resource):
        def decorate(view_class):
            self.register(resource, view_class)
            return view_class

        return decorate

    def unregister(self, resource):
        name = resource.name if isinstance(resource, type) else resource.name
        self._declarations.pop(name, None)
        self._registry = {
            key: view for key, view in self._registry.items() if view.resource.name != name
        }

    def finalize(self):
        from obox_builder.resources import ReadOnlyModelResource

        registry = self.resource_registry
        compiled = {}
        for name, (definition, view_class) in self._declarations.items():
            resource = registry.resources.get(name)
            if resource is None:
                continue
            if not isinstance(resource, ReadOnlyModelResource):
                raise ImproperlyConfigured("ModelView requires a model resource.")
            if type(resource) is not definition:
                raise ImproperlyConfigured(
                    f"View binding for {name!r} does not match its resource definition."
                )
            current = self._registry.get(model_label_for(resource.model))
            instance = (
                current
                if current is not None
                and current.resource is resource
                and type(current) is view_class
                else view_class(resource, self)
            )
            compiled[instance.model_label] = instance
        self._registry = compiled

    def get_registry(self) -> dict[str, ModelView]:
        return dict(self._registry)

    def get_model_admin(self, app_label: str, model_name: str) -> ModelView | None:
        return self._registry.get(f"{app_label}.{model_name}") or self._registry.get(
            f"{app_label}.{camel_to_snake(model_name)}"
        )

    def get_model_admin_for(self, model: type[Any]) -> ModelView | None:
        return self._registry.get(model_label_for(model))

    # -- discovery ----------------------------------------------------------
    def autodiscover(self) -> None:
        """Import ``admin.py`` from every installed app (idempotent)."""
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
                            required_perm="jobs.task.read",
                        ),
                        NavItem(
                            "Workers",
                            "/admin/jobs/workers",
                            icon="ti-server",
                            required_perm="jobs.worker.read",
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
        self.finalize()
        return build_router(self)

    # -- tests --------------------------------------------------------------
    def _reset(self) -> None:
        self._registry.clear()
        self._declarations.clear()
        self._discovered = False
        self.navigation = default_navigation()


def model_label_for(model: type[Any]) -> str:
    app_label = getattr(model, "_model_label", "") or getattr(model, "app_label", "")
    return f"{app_label}.{camel_to_snake(model.__name__)}"


site = AdminSite(import_path="obox_builder.contrib.admin.site.site")
