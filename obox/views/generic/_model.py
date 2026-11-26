from collections.abc import Mapping
from typing import Any, ClassVar

from fastapi import Request

from obox.core.exceptions import ImproperlyConfigured
from obox.core.principal import has_permission
from obox.db.identity import model_label
from obox.resources import ModelResource, resolve_resource
from obox.views.generic.base import ContextMixin, TemplateResponseMixin


def model_permission(model: type[Any], operation: str) -> str:
    label, name = model_label(model)
    return f"{label}.{operation}_{name}"


class ModelContextMixin(ContextMixin):
    model: type[Any] | None = None
    resource: type[ModelResource[Any]] | ModelResource[Any] | None = None
    route_names: ClassVar[Mapping[str, str]] = {}
    request: Request

    session: Any

    def check(self) -> None:
        super().check()
        if self.resource is not None and self.model is not None:
            raise ImproperlyConfigured("Specify either model or resource.")
        if isinstance(self.resource, type):
            self.resource.check()
        elif self.resource is not None and not isinstance(self.resource, ModelResource):
            raise ImproperlyConfigured("Provide a ModelResource class.")
        elif self.model is not None:
            resolve_resource(self.model)

    def bound_resource(self) -> ModelResource[Any] | None:
        if isinstance(self.resource, type):
            if self.model is not None:
                raise ImproperlyConfigured("Specify either model or resource.")
            self.resource = resolve_resource(self.resource)
        if (
            self.resource is not None
            and getattr(self, "session", None) is not None
            and (
                self.resource.session is not self.session
                or self.resource.principal is not self.request.user
            )
        ):
            self.resource = self.resource.bind(self.session, self.request.user)
        return self.resource

    def has_model_permission(self, model: type[Any], operation: str) -> bool:
        return has_permission(self.request.user, model_permission(model, operation))

    def route_url(self, operation: str, **params: Any) -> str | None:
        if name := self.route_names.get(operation):
            path = str(self.request.app.url_path_for(name, **params))
            return str(self.request.scope.get("root_path", "")).rstrip("/") + path
        return None


class ModelTemplateMixin(TemplateResponseMixin):
    template_prefix = ""
    template_name_suffix = ""

    def model_template_names(self, model: type[Any], fallback: str) -> list[str]:
        app, name = model_label(model)
        table = getattr(model, "__tablename__", name)
        derived = f"{self.template_prefix}{table}{self.template_name_suffix}.html"
        legacy = f"{app}/{name}{self.template_name_suffix}.html"
        alternatives = [derived, legacy] if self.template_prefix else [legacy, derived]
        return [*alternatives, f"obox/generic/{fallback}.html"]
