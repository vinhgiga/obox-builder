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
from obox.resources import ModelResource, WriteAction
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
            "operators": (
                ["is_empty", "is_not_empty"]
                if "obox_files" in column.info
                else filter_operators(kind)
            ),
            "nullable": bool(column.nullable),
            "required": spec.required,
        }
    return options


def model_permission(model: type[Any], operation: str) -> str:
    label, name = model_label(model)
    return f"{label}.{operation}_{name}"


class ModelContextMixin(ContextMixin, Generic[ModelT]):
    resource: type[ModelResource[ModelT]] | ModelResource[ModelT]
    route_names: ClassVar[Mapping[str, str]] = {}
    request: Request

    session: Any
    reverse: Any

    def check(self) -> None:
        super().check()
        if isinstance(getattr(self, "resource", None), type):
            self.resource.check()
        elif not isinstance(getattr(self, "resource", None), ModelResource):
            raise ImproperlyConfigured("Define a ModelResource.")

    def bound_resource(self) -> ModelResource[ModelT]:
        if isinstance(self.resource, type):
            self.resource = self.resource()
        if getattr(self, "session", None) is not None:
            self.resource = self.resource.bind(self.session, self.request.user)
        return self.resource

    def has_model_permission(self, model: type[Any], operation: str) -> bool:
        return has_permission(self.request.user, model_permission(model, operation))

    def route_url(self, operation: str, **params: Any) -> str | None:
        if name := self.route_names.get(operation):
            return str(self.reverse(name, **params))
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
        table = model.__tablename__
        suffix = self.template_name_suffix if suffix is None else suffix
        derived = f"{self.template_prefix}{table}{suffix}.html"
        return [derived, f"obox/generic/{fallback}.html"]
