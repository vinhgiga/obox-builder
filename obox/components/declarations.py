"""Reusable component declarations and immutable placements."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Mapping
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from pydantic import Field, JsonValue, PrivateAttr, StrictStr

from obox.core.exceptions import ImproperlyConfigured
from obox.core.principal import valid_permission
from obox.db.filters import FilterGroup, parse_filter
from obox.runtime.context import Context
from obox.web.schemas import ApiRequest, ApiResponse

if TYPE_CHECKING:
    from obox.components.service import ComponentService


class ComponentQuery(ApiRequest):
    _options: dict[str, Any] | None = PrivateAttr(default=None)
    query: list[tuple[StrictStr, StrictStr]] = Field(default_factory=list)


class ComponentRecords(ComponentQuery):
    selector: str | None = None
    row: JsonValue = None
    column: JsonValue = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)


class RecordResults(ApiResponse):
    _objects: list[Any] = PrivateAttr(default_factory=list)
    records: list[dict[str, JsonValue]]
    offset: int
    limit: int
    has_more: bool


class ComponentDescription(ApiResponse):
    name: str
    title: str
    kind: str
    description: str
    capabilities: list[str]
    query_schema: dict[str, Any] | None
    result_schema: dict[str, Any] | None
    presentation: dict[str, Any]


class ComponentDescriptions(ApiResponse):
    components: list[ComponentDescription]


class Component:
    _dataset: Any | None = None
    name: str | None = None
    source: type[Any] | None = None
    fields: tuple[str, ...] = ()
    filter_fields: tuple[str, ...] | None = None
    ordering: tuple[str, ...] = ()

    def get_queryset(self, ctx: Context) -> Any:
        import sqlalchemy as sa

        if self.source is None:
            raise ImproperlyConfigured("Declare a source model for this component.")
        return sa.select(self.source)

    def get_fields(self, ctx: Context) -> tuple[str, ...]:
        return self.fields

    def has_permission(self, ctx: Context) -> bool:
        return True

    def get_record_fields(self, ctx: Context, obj: Any) -> tuple[str, ...]:
        return self.get_fields(ctx)

    def has_object_permission(self, ctx: Context, obj: Any) -> bool:
        return True

    title = ""
    description = ""
    kind = "custom"
    template: str | None = None
    permission: str | None = None
    records_permission: str | None = None
    query_model: type[ApiRequest] | None = None
    result_model: type[ApiResponse] | None = None
    filters: FilterGroup | Mapping[str, Any] = FilterGroup()
    required_fields: tuple[str, ...] = ()
    plugin: str | None = None

    @property
    def dataset(self) -> Any:
        resource = self._dataset
        if resource is None:
            raise ImproperlyConfigured("This execution has no bound source resource.")
        return resource

    def __init__(self, **options: Any) -> None:
        for name, value in options.items():
            if (
                name.startswith("_")
                or not hasattr(type(self), name)
                or (
                    callable(getattr(type(self), name))
                    and name not in {"source", "query_model", "result_model"}
                )
            ):
                raise ImproperlyConfigured(f"Unknown component option: {name}.")
            setattr(self, name, deepcopy(value))

    def get_context(self, ctx: Context) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        return {}

    def get_data(self, query: Any, ctx: Context) -> Any:
        raise ImproperlyConfigured("Declare get_data for an API component.")

    def snapshot(self) -> Component:
        values: dict[str, Any] = {}
        for base in reversed(type(self).__mro__):
            values.update(
                (name, value)
                for name, value in vars(base).items()
                if not name.startswith("_")
                and not isinstance(value, (property, classmethod, staticmethod))
                and (not callable(value) or name in {"source", "query_model", "result_model"})
            )
        values.update(vars(self))
        result = object.__new__(type(self))
        vars(result).update(deepcopy(values))
        return result

    def build(self, builder: Any, name: str) -> Any:
        return None

    async def execute(self, service: ComponentService, query: ApiRequest, ctx: Context) -> Any:
        from obox.core.async_utils import invoke

        return await invoke(self.get_data, query, ctx)

    def presentation(self, spec: Any) -> dict[str, Any]:
        return {}

    async def records(
        self, service: ComponentService, query: ComponentRecords, ctx: Context
    ) -> Any:
        from obox.core.exceptions import NotFound

        raise NotFound("Record inspection is disabled.")


def component_name(name: Any) -> None:
    if not isinstance(name, str) or re.fullmatch(r"[a-z][a-zA-Z0-9]*", name) is None:
        raise ImproperlyConfigured("Component names must be camelCase identifiers.")


def fixed_filters(value: FilterGroup | Mapping[str, Any]) -> FilterGroup:
    import json

    from obox.core.exceptions import DomainError

    if isinstance(value, Mapping):
        from obox.db.filters import FilterTerm

        terms = []
        for lookup, operand in value.items():
            if not isinstance(lookup, str) or not lookup:
                raise ImproperlyConfigured("Fixed filter fields must be nonempty strings.")
            name, separator, operator = lookup.rpartition("__")
            terms.append(
                FilterTerm(name if separator else lookup, operator if separator else "is", operand)
            )
        value = FilterGroup(children=tuple(terms))
    if not isinstance(value, FilterGroup):
        raise ImproperlyConfigured("Component filters require a FilterGroup or mapping.")
    try:
        parse_filter(json.dumps(value.to_dict()))
    except (DomainError, TypeError, ValueError, RecursionError) as exc:
        raise ImproperlyConfigured("Invalid fixed component filters.") from exc
    return deepcopy(value)


def validate_component(component: Component) -> None:
    for name in ("permission", "records_permission"):
        value = getattr(component, name)
        if value is not None and (not isinstance(value, str) or not valid_permission(value)):
            raise ImproperlyConfigured("Component permissions require app.resource.action.")
    if not isinstance(component.title, str) or not isinstance(component.description, str):
        raise ImproperlyConfigured("Component titles and descriptions must be strings.")
    if component.template is not None and (
        not isinstance(component.template, str)
        or not component.template.strip()
        or component.template.startswith("/")
        or "\\" in component.template
        or ".." in component.template.split("/")
    ):
        raise ImproperlyConfigured("Component templates require a logical template name.")
    if (component.query_model is None) != (component.result_model is None):
        raise ImproperlyConfigured("API components require query_model and result_model.")
    for name, base in (("query_model", ApiRequest), ("result_model", ApiResponse)):
        value = getattr(component, name)
        if value is not None and (not isinstance(value, type) or not issubclass(value, base)):
            raise ImproperlyConfigured(f"Component.{name} must inherit {base.__name__}.")
    if not component.template and component.query_model is None:
        raise ImproperlyConfigured("Components require a template or a typed data provider.")
