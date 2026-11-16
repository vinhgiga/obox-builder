"""Component binding and authorized execution, independent of HTTP and rendering."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, cast

from pydantic import ValidationError

from obox._auth import has_permission
from obox._components import (
    Component,
    ComponentDescription,
    ComponentQuery,
    ComponentRecords,
    RecordResults,
    component_name,
    fixed_filters,
    validate_component,
)
from obox._context import Context
from obox._exceptions import BuilderValidationError, NotAuthenticated, NotFound, PermissionDenied
from obox._pages import Page
from obox.contrib.resources.filters import FilterGroup, FilterTerm
from obox.contrib.resources.query import CONTROLS, parse_terms
from obox.contrib.resources.service import ResourceService, _filter_tree, invoke
from obox.core.exceptions import ImproperlyConfigured


class ComponentResource(ResourceService[Any]):
    """Read authority is independent of whether CRUD routes are published."""

    def __init__(self, builder: Any, app: Any, filters: FilterGroup) -> None:
        super().__init__(builder, app)
        self.filters = filters

    async def permitted(self, action: str, ctx: Context, obj: Any = None) -> bool:
        self._context(ctx)
        if action not in {"list", "retrieve"}:
            return False
        scopes = getattr(ctx.principal, "api_scopes", None)
        if scopes is not None and self.builder.permission_name(action) not in scopes:
            return False
        return bool(await invoke(self.builder.policy.allow, action, ctx, obj=obj))

    def _statement(self, action: str, ctx: Context) -> Any:
        statement = super()._statement(action, ctx)
        readable = set(self.builder.readable(ctx)).intersection(self.builder.api_read)
        condition = _filter_tree(
            self.builder, readable, self.filters, ctx.session.get_bind().dialect.name
        )
        return statement.where(condition) if condition is not None else statement


@dataclass(frozen=True)
class ComponentService:
    app: Any
    app_label: str
    name: str
    _prototype: Component
    resource: ComponentResource | None
    spec: Any
    page: Page | None = None

    @classmethod
    def bind(
        cls,
        app: Any,
        declaration: type[Component] | Component,
        *,
        app_label: str,
        name: str | None = None,
        page: Page | None = None,
        title: str | None = None,
        filters: Any = None,
    ) -> ComponentService:
        from obox.contrib.resources import Resource

        if isinstance(declaration, type) and issubclass(declaration, Component):
            declaration = declaration()
        if not isinstance(declaration, Component):
            raise ImproperlyConfigured("Declare a Component class or instance.")
        if not callable(app.settings.principal_dependency):
            raise ImproperlyConfigured("Components require an identity resolver.")
        component = declaration._snapshot()
        name = name or component.name
        component_name(name)
        component.name = cast(str, name)
        if title is not None:
            component.title = title
        component.title = component.title or re.sub(r"(?<!^)(?=[A-Z])", " ", component.name).title()
        validate_component(component)
        if component.plugin and app._feature(component.plugin) is None:
            raise ImproperlyConfigured(f"Install {component.plugin} for this component.")
        predicate = fixed_filters(component.filters)
        if filters is not None:
            predicate = FilterGroup(children=(predicate, fixed_filters(filters)))
        component.filters = predicate
        resource = None
        if component.source is not None:
            source = component.source
            if not isinstance(source, type) or not issubclass(source, Resource):
                raise ImproperlyConfigured("Component.source must be a registered Resource.")
            try:
                builder = app.get_resource(source._model_type())
            except KeyError as exc:
                raise ImproperlyConfigured("Component source is not registered.") from exc
            if not isinstance(builder, source):
                raise ImproperlyConfigured("Use the registered source builder.")
            resource = ComponentResource(builder, app, predicate)
            # Validate fixed filters without executing SQL or opening a session.
            try:
                _filter_tree(builder, set(builder.fields or ()), predicate, "sqlite")
            except BuilderValidationError as exc:
                raise ImproperlyConfigured("Invalid fixed component filter.") from exc
        elif component.plugin == "analytics" or predicate.children:
            raise ImproperlyConfigured("Data filters require a registered source.")
        spec = component._build(resource.builder if resource else None, component.name)
        if (
            component.query_model is not None
            and type(component)._execute is Component._execute
            and type(component).get_data is Component.get_data
        ):
            raise ImproperlyConfigured("ApiOptions components require get_data.")
        return cls(app, app_label, component.name, component, resource, spec, page)

    @property
    def identity(self) -> str:
        return f"{self.app_label}.{self.page.name + '.' if self.page else ''}{self.name}"

    @property
    def definition(self) -> Component:
        return deepcopy(self._prototype)

    @property
    def query_model(self) -> Any:
        return self._prototype.query_model

    @property
    def result_model(self) -> Any:
        return self._prototype.result_model

    def _context(self, ctx: Context) -> Context:
        if (
            ctx._owner is not self.app
            or not ctx._active
            or ctx._failed
            or ctx.session.sync_session.get_transaction() is not ctx._transaction
        ):
            raise ImproperlyConfigured("Use an active unit of work from this application.")
        # Component projections retain the ApiOptions field restrictions on every surface.
        return replace(ctx, source="api")

    async def authorize(self, ctx: Context, *, records: bool = False) -> Context:
        ctx = self._context(ctx)
        actor = ctx.principal
        if not getattr(actor, "is_authenticated", False) or not getattr(actor, "is_active", False):
            raise NotAuthenticated("Authentication is required.")
        if self.page is not None and not self.page.permitted(actor):
            raise PermissionDenied("This page is not permitted.")
        definition = self._prototype
        for permission in (
            definition.permission,
            definition.records_permission if records else None,
        ):
            if permission is not None and not has_permission(actor, permission):
                raise PermissionDenied("This component is not permitted.")
        if self.resource is not None:
            await self.resource.authorize("list", ctx)
            readable = set(self.resource.builder.readable(ctx)).intersection(
                self.resource.builder.api_read
            )
            fields = set(definition.required_fields)
            if self.spec is not None:
                fields.update(
                    self.spec.fields
                    if hasattr(self.spec, "fields")
                    else self.spec.aggregation.fields
                )
                readable.intersection_update(self.resource.builder.policy.aggregate_fields(ctx))
            if not fields.issubset(readable):
                raise PermissionDenied("These fields are not available for this component.")
            # Recheck dynamic field restrictions on fixed filters.
            self.resource._statement("list", ctx)
        return ctx

    async def permitted(self, ctx: Context) -> bool:
        try:
            await self.authorize(ctx)
        except (NotAuthenticated, PermissionDenied):
            return False
        return True

    def options(self, query: ComponentQuery) -> dict[str, Any]:
        if query._options is not None:
            return query._options
        if self.resource is None:
            return {}
        for key, _ in query.query:
            if key in CONTROLS - {"search"}:
                raise BuilderValidationError(
                    {key: ["Component shape and ordering are defined on the server."]}
                )
        params, predicate = parse_terms(query.query)
        names = {alias: name for name, alias in self.resource.builder.api_names.items()}

        def resolve(node: FilterTerm | FilterGroup) -> FilterTerm | FilterGroup:
            if isinstance(node, FilterTerm):
                if node.field not in names:
                    raise BuilderValidationError({node.field: ["Unknown ApiOptions field."]})
                return replace(node, field=names[node.field])
            return replace(node, children=tuple(resolve(child) for child in node.children))

        return {"filter_group": resolve(predicate), "search": params.get("search")}

    def _input(self, data: Any) -> Any:
        if self.query_model is None:
            raise NotFound("This component has no data ApiOptions.")
        try:
            return (
                data
                if isinstance(data, self.query_model)
                else self.query_model.model_validate(data)
            )
        except ValidationError as exc:
            raise BuilderValidationError(
                {"query": [error["msg"] for error in exc.errors()]}
            ) from exc

    async def query(self, data: Any = None, *, ctx: Context) -> Any:
        ctx = await self.authorize(ctx)
        query = self._input({} if data is None else data)
        component = self.definition
        component._resource = self.resource
        result = await component._execute(self, query, ctx)
        self._context(ctx)
        try:
            return self.result_model.model_validate(result)
        except ValidationError as exc:
            raise ImproperlyConfigured("Component returned an invalid result.") from exc

    async def records(
        self, data: ComponentRecords | Mapping[str, Any], *, ctx: Context
    ) -> RecordResults:
        ctx = await self.authorize(ctx, records=True)
        try:
            query = (
                data
                if isinstance(data, ComponentRecords)
                else ComponentRecords.model_validate(data)
            )
        except ValidationError as exc:
            raise BuilderValidationError(
                {"records": [error["msg"] for error in exc.errors()]}
            ) from exc
        result = await self.definition._records(self, query, ctx)
        self._context(ctx)
        return RecordResults.model_validate(result)

    async def context(self, *, ctx: Context) -> Mapping[str, Any]:
        ctx = await self.authorize(ctx)
        component = self.definition
        component._resource = self.resource
        values = await invoke(component.get_context, ctx)
        self._context(ctx)
        if not isinstance(values, Mapping):
            raise ImproperlyConfigured("Component context must return a mapping.")
        return values

    async def describe(self, *, ctx: Context) -> ComponentDescription:
        await self.authorize(ctx)
        definition = self._prototype
        capabilities = ["query"] if self.query_model else []
        drill = getattr(definition, "drill", False) and (
            definition.records_permission is None
            or has_permission(ctx.principal, definition.records_permission)
        )
        if drill:
            capabilities.append("records")
        if definition.template or definition.plugin == "analytics":
            capabilities.append("view")
        presentation = definition._presentation(self.spec)
        if "drill" in presentation:
            presentation["drill"] = drill
        return ComponentDescription(
            name=self.name,
            title=definition.title,
            kind=definition.kind,
            description=definition.description,
            capabilities=capabilities,
            query_schema=self.query_model.model_json_schema(by_alias=True)
            if self.query_model
            else None,
            result_schema=self.result_model.model_json_schema(by_alias=True)
            if self.result_model
            else None,
            presentation=presentation,
        )
