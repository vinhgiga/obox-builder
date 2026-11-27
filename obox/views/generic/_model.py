from collections.abc import Mapping
from typing import Any, ClassVar, Generic, TypedDict

import sqlalchemy as sa
from fastapi import Request
from typing_extensions import TypeVar

from obox.core.async_utils import invoke
from obox.core.exceptions import ImproperlyConfigured
from obox.core.principal import has_permission
from obox.db.identity import model_label
from obox.db.query import field_kind, filter_operators
from obox.resources import ModelResource, WriteAction, resolve_resource
from obox.views.generic.base import ContextMixin, TemplateResponseMixin

ModelT = TypeVar("ModelT", default=Any)


class Column(TypedDict):
    name: str
    label: str
    kind: str
    choices: list[str]
    operators: list[str]
    nullable: bool
    required: bool


def field_options(resource: ModelResource[Any]) -> dict[str, Column]:
    options: dict[str, Column] = {}
    for name in resource.readable:
        spec = resource.specs[name]
        column = spec.column
        kind = field_kind(column)
        options[name] = {
            "name": name,
            "label": name.replace("_", " ").capitalize(),
            "kind": kind,
            "choices": list(column.type.enums) if isinstance(column.type, sa.Enum) else [],
            "operators": filter_operators(kind),
            "nullable": bool(column.nullable),
            "required": spec.required,
        }
    return options


def reject_options(view: object, names: tuple[str, ...]) -> None:
    if removed := [name for name in names if hasattr(view, name)]:
        raise ImproperlyConfigured(f"{type(view).__name__} removed options: {', '.join(removed)}.")


def model_permission(model: type[Any], operation: str) -> str:
    label, name = model_label(model)
    return f"{label}.{operation}_{name}"


class ModelContextMixin(ContextMixin, Generic[ModelT]):
    model: type[ModelT] | None = None
    resource: type[ModelResource[ModelT]] | ModelResource[ModelT] | None = None
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

    def bound_resource(self) -> ModelResource[ModelT] | None:
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

    def model_resource(self, model: type[ModelT]) -> ModelResource[ModelT]:
        return self.bound_resource() or resolve_resource(model)

    def has_model_permission(self, model: type[Any], operation: str) -> bool:
        return has_permission(self.request.user, model_permission(model, operation))

    def route_url(self, operation: str, **params: Any) -> str | None:
        if name := self.route_names.get(operation):
            path = str(self.request.app.url_path_for(name, **params))
            return str(self.request.scope.get("root_path", "")).rstrip("/") + path
        return None

    async def write_url(
        self, action: WriteAction, resource: ModelResource[ModelT], obj: ModelT | None = None
    ) -> str | None:
        permission = {"create": "add", "update": "change", "delete": "delete"}[action]
        if (
            action not in self.route_names
            or not self.has_model_permission(resource.model, permission)
            or not await invoke(resource.can, action, obj, self.request.user)
        ):
            return None
        params = {"pk": str(getattr(obj, resource.lookup_name))} if obj is not None else {}
        return self.route_url(action, **params)


class ModelTemplateMixin(TemplateResponseMixin):
    template_prefix = ""
    template_name_suffix = ""

    def model_template_names(
        self, model: type[Any], fallback: str, *, suffix: str | None = None
    ) -> list[str]:
        app, name = model_label(model)
        table = getattr(model, "__tablename__", name)
        suffix = self.template_name_suffix if suffix is None else suffix
        derived = f"{self.template_prefix}{table}{suffix}.html"
        legacy = f"{app}/{name}{suffix}.html"
        alternatives = [derived, legacy] if self.template_prefix else [legacy, derived]
        return [*alternatives, f"obox/generic/{fallback}.html"]
