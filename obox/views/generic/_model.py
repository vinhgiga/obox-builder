from collections.abc import Mapping
from typing import Any, ClassVar

from fastapi import Request

from obox._auth import has_permission
from obox.core.exceptions import ImproperlyConfigured
from obox.db._identity import _model_label
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

    def _bound_resource(self) -> Resource[Any] | None:
        if isinstance(self.resource, type):
            if self.model is not None:
                raise ImproperlyConfigured("Specify either model or resource.")
            self.resource = resolve_resource(self.resource)
        return self.resource

    def _has_model_permission(self, model: type[Any], operation: str) -> bool:
        return has_permission(self.request.user, model_permission(model, operation))

    def _route_url(self, operation: str, **params: Any) -> str | None:
        if name := self.route_names.get(operation):
            return str(self.request.url_for(name, **params))
        return None
