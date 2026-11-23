from collections.abc import Mapping
from typing import Any, ClassVar

from fastapi import Request

from obox.core.exceptions import ImproperlyConfigured
from obox.core.principal import has_permission
from obox.db.identity import _model_label
from obox.resources import Resource, resolve_resource
from obox.views.generic.base import ContextMixin


def model_permission(model: type[Any], operation: str) -> str:
    label, name = _model_label(model)
    return f"{label}.{operation}_{name}"


class ModelContextMixin(ContextMixin):
    model: type[Any] | None = None
    resource: type[Resource[Any]] | Resource[Any] | None = None
    route_names: ClassVar[Mapping[str, str]] = {}
    request: Request

    session: Any

    def check(self) -> None:
        super().check()
        if self.resource is not None and self.model is not None:
            raise ImproperlyConfigured("Specify either model or resource.")
        if isinstance(self.resource, type):
            self.resource.check()
        elif self.resource is not None and not isinstance(self.resource, Resource):
            raise ImproperlyConfigured("Provide a Resource class.")
        elif self.model is not None:
            resolve_resource(self.model)

    def _bound_resource(self) -> Resource[Any] | None:
        if isinstance(self.resource, type):
            if self.model is not None:
                raise ImproperlyConfigured("Specify either model or resource.")
            self.resource = resolve_resource(self.resource)
        if (
            self.resource is not None
            and getattr(self, "session", None) is not None
            and self.resource.session is not self.session
        ):
            self.resource = self.resource.bind(self.session, self.request.user)
        return self.resource

    def _has_model_permission(self, model: type[Any], operation: str) -> bool:
        return has_permission(self.request.user, model_permission(model, operation))

    def _route_url(self, operation: str, **params: Any) -> str | None:
        if name := self.route_names.get(operation):
            path = str(self.request.app.url_path_for(name, **params))
            return str(self.request.scope.get("root_path", "")).rstrip("/") + path
        return None
