from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, cast

import sqlalchemy as sa
from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from starlette.exceptions import HTTPException

from obox.core.async_utils import invoke
from obox.core.exceptions import ImproperlyConfigured, InvalidQuery, PermissionDenied
from obox.db.identity import _model_label
from obox.db.query import (
    _column,
    _field_kind,
    _filter_expression,
    _filter_operators,
    _parse_order,
    list_query,
    search_query,
)
from obox.db.session import _session
from obox.resources import resolve_resource
from obox.views.generic._model import ModelContextMixin
from obox.views.generic.base import TemplateResponseMixin, TemplateView, View
from obox.views.generic.decorators import action
from obox.web.http import read_form


class ListView(TemplateResponseMixin, ModelContextMixin, View):
    queryset: Any = None
    context_object_name: str | None = None
    paginate_by: int | None = None
    page_kwarg = "page"
    page_start = 1
    allow_empty = True
    ordering: Any = None
    template_name_suffix = "_list"
    object_list: Any
    list_display: Sequence[Any] | None = None
    list_display_links: Sequence[Any] | None = ()
    list_filter: Sequence[str] = ()
    search_fields: Sequence[str] = ()
    actions: Sequence[Any] | None = ()
    add_url: str | None = None

    def check(self) -> None:
        super().check()
        if self.paginate_by is not None and (
            type(self.paginate_by) is not int or self.paginate_by < 1
        ):
            raise ImproperlyConfigured("Choose a positive page size.")
        if (
            self.model is None
            and self.resource is None
            and self.queryset is None
            and type(self).get_queryset is ListView.get_queryset
        ):
            raise ImproperlyConfigured("Define model, resource or queryset.")

    def get_ordering(self) -> Any:
        return self.ordering

    def _queryable_fields(self, model: type[Any]) -> set[str]:
        return {
            name
            for name in self.get_list_display()
            if isinstance(name, str) and _column(model, name) is not None
        }

    def _filter_field_options(self, model: type[Any]) -> list[dict[str, Any]]:
        options = []
        for name in self.get_list_display():
            if not isinstance(name, str) or (column := _column(model, name)) is None:
                continue
            kind = _field_kind(column)
            options.append(
                {
                    "name": name,
                    "label": name.replace("_", " ").capitalize(),
                    "kind": kind,
                    "choices": list(column.type.enums) if isinstance(column.type, sa.Enum) else [],
                    "operators": _filter_operators(kind),
                }
            )
        return options

    def get_queryset(self) -> Any:
        if resource := self._bound_resource():
            query = resource.get_queryset().order_by(*resource.get_ordering())
        elif self.queryset is not None:
            query = self.queryset._generate()
        elif self.model is not None:
            query = sa.select(self.model)
        else:
            raise ImproperlyConfigured("Define model, queryset, or get_queryset().")
        model = query.column_descriptions[0]["entity"]
        queryable = self._queryable_fields(model)
        if resource is None and (ordering := self.get_ordering()):
            names = (ordering,) if isinstance(ordering, str) else ordering
            query = query.order_by(
                *(
                    getattr(model, name.lstrip("-")).desc()
                    if name.startswith("-")
                    else getattr(model, name)
                    for name in names
                )
            )
        if not hasattr(self, "request"):
            return query
        if self.search_fields:
            query = self.get_search_results(query, self.request.query_params.get("q", ""))
        return list_query(
            query,
            model,
            self.request.query_params,
            filters=self.get_list_filter(),
            allowed=queryable,
        )

    def get_list_display(self) -> Sequence[Any]:
        resource = self._bound_resource()
        if self.list_display is None:
            return resource.readable if resource is not None else ()
        if resource is None:
            return self.list_display
        return tuple(
            name
            for name in self.list_display
            if callable(name) or name not in resource.specs or name in resource.readable
        )

    def get_list_filter(self) -> Sequence[str]:
        resource = self._bound_resource()
        if resource is None:
            return self.list_filter
        return tuple(name for name in self.list_filter if name in resource.readable)

    def get_search_results(self, queryset: Any, term: str) -> Any:
        resource = self._bound_resource()
        model = queryset.column_descriptions[0]["entity"]
        fields = tuple(
            name
            for name in self.search_fields
            if resource is None or name.lstrip("^=") in resource.readable
        )
        return search_query(queryset, model, fields, term)

    def get_actions(self, model: type[Any]) -> dict[str, tuple[Any, str, str]]:
        allowed = {}
        for declaration in self.actions or ():
            method = (
                getattr(type(self), declaration) if isinstance(declaration, str) else declaration
            )
            name = method.__name__
            permissions = getattr(method, "allowed_permissions", ("change",))
            if any(self._has_model_permission(model, permission) for permission in permissions):
                label = getattr(method, "short_description", name.replace("_", " ").capitalize())
                allowed[name] = (method, name, label)
        return allowed

    @action(permissions=("delete",), description="Delete selected objects")
    async def delete_selected(self, request: Request, queryset: Any) -> Response | None:
        resource = self._bound_resource()
        if resource is None:
            raise ImproperlyConfigured("Bind a Resource to delete selected records.")
        data = await read_form(request)
        if data.get("post") != "yes":
            view = TemplateView(template_name="obox/generic/delete_selected.html")
            view.setup(request)
            return view.render_to_response(
                {"selected": data.getlist("_selected_action"), "action": "delete_selected"}
            )
        async with _session() as session:
            bound = resource.bind(session, request.user)
            for obj in (await bound.list(queryset=queryset.with_for_update())).items:
                await bound.destroy(obj)
        return None

    async def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        if not self.actions:
            return self.http_method_not_allowed(request, *args, **kwargs)
        data = await read_form(request)
        queryset = await invoke(self.get_queryset)
        model = queryset.column_descriptions[0]["entity"]
        if selected_action := self.get_actions(model).get(str(data.get("action", ""))):
            column = sa.inspect(model).primary_key[0]
            try:
                selected = [
                    column.type.python_type(value) for value in data.getlist("_selected_action")
                ]
            except (TypeError, ValueError) as exc:
                raise PermissionDenied("Invalid selection.") from exc
            result = await invoke(
                selected_action[0], self, request, queryset.where(column.in_(selected))
            )
            if isinstance(result, Response):
                return result
        return RedirectResponse(str(request.url), status_code=303)

    def _allowed_methods(self) -> list[str]:
        methods = super()._allowed_methods()
        if not self.actions and type(self).post is ListView.post and "POST" in methods:
            methods.remove("POST")
        return methods

    def _rows(self, model: type[Any], columns: Sequence[Any]) -> list[dict[str, Any]]:
        mapper = sa.inspect(model)
        pk = mapper.get_property_by_column(mapper.primary_key[0]).key
        rows = []
        for obj in self.object_list:
            values = []
            for name in columns:
                display = getattr(self, name, None) if isinstance(name, str) else name
                value = (
                    display(obj) if callable(display) and name != "__str__" else getattr(obj, name)
                )
                values.append(value() if callable(value) else value)
            row_id = getattr(obj, pk)
            rows.append(
                {"pk": row_id, "values": values, "url": self._route_url("detail", pk=str(row_id))}
            )
        return rows

    def _page_url(self, number: int) -> str:
        return str(
            self.request.url.include_query_params(**{self.page_kwarg: number - 1 + self.page_start})
        )

    def get_context_data(self, **kwargs: Any) -> Any:
        model = self.model or self._list_model
        columns = self.get_list_display()
        queryable = self._queryable_fields(model)
        filter_config: dict[str, Any] = {
            "fields": self._filter_field_options(model),
            "filter": {"conjunction": "and", "children": []},
        }
        raw_filter = self.request.query_params.get("filter")
        if raw_filter:
            try:
                filter_data = json.loads(raw_filter)
                cleaned, _, _ = _filter_expression(model, filter_data, queryable)
                filter_config["filter"] = cleaned
            except (TypeError, ValueError, InvalidQuery):
                filter_config["filter"] = {"conjunction": "and", "children": []}
        exact_terms = [
            {"field": name, "op": "is", "value": value}
            for name, value in self.request.query_params.items()
            for name in [name.removesuffix("__exact")]
            if name in self.get_list_filter() and value != "" and name in queryable
        ]
        if exact_terms:
            filter_config["filter"]["children"].extend(exact_terms)
        sort_config = {
            "fields": filter_config["fields"],
            "sort": _parse_order(
                self.request.query_params.get("ordering", ""), queryable, maximum=5
            ),
        }
        group_config = {
            "fields": filter_config["fields"],
            "group": _parse_order(self.request.query_params.get("group", ""), queryable, maximum=3),
        }
        page = self._page
        context = {
            "object_list": self.object_list,
            self.context_object_name or model.__name__.lower() + "_list": self.object_list,
            "is_paginated": page is not None and page.num_pages > 1,
            "page_obj": page,
            "title": model.__name__,
            "list_display": columns,
            "rows": self._rows(model, columns) if columns or self.actions else [],
            "list_display_links": None
            if self.list_display_links is None
            else self.list_display_links or columns[:1],
            "actions": self.get_actions(model),
            "search_fields": self.search_fields,
            "search_term": self.request.query_params.get("q", ""),
            "filters": [
                (name, self.request.query_params.get(name + "__exact", ""))
                for name in self.get_list_filter()
            ],
            "filter_config": filter_config,
            "sort_config": sort_config,
            "group_config": group_config,
            "previous_url": self._page_url(page.number - 1) if page and page.has_previous else None,
            "next_url": self._page_url(page.number + 1) if page and page.has_next else None,
            "add_url": self.add_url,
        }
        return cast(dict[str, Any], super().get_context_data(**{**context, **kwargs}))

    def get_template_names(self) -> list[str]:
        if self.template_name:
            return [self.template_name]
        app, name = _model_label(self.model or self._list_model)
        return [f"{app}/{name}{self.template_name_suffix}.html", "obox/generic/list.html"]

    async def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        query = await invoke(self.get_queryset)
        self._list_model = query.column_descriptions[0]["entity"]
        self._page = None
        number = 1
        resource = self._bound_resource()
        if (
            "create" in self.route_names
            and self._has_model_permission(self._list_model, "add")
            and (resource is None or await invoke(resource.can, "create", None, request.user))
        ):
            self.add_url = self._route_url("create")
        async with _session() as session:
            bound = (resource or resolve_resource(self._list_model)).bind(session, request.user)
            if self.paginate_by:
                raw = kwargs.get(
                    self.page_kwarg, request.query_params.get(self.page_kwarg, str(self.page_start))
                )
                try:
                    if raw == "last" and self.page_start == 1:
                        first = await bound.paginate(1, page_size=self.paginate_by, queryset=query)
                        number = first.num_pages
                    else:
                        number = int(raw) - self.page_start + 1
                except (TypeError, ValueError) as exc:
                    raise HTTPException(404, "Invalid page.") from exc
                page = await bound.paginate(number, page_size=self.paginate_by, queryset=query)
                self.object_list, self._page = page.items, page
            else:
                self.object_list = (await bound.list(queryset=query)).items
        if not self.allow_empty and not self.object_list:
            raise HTTPException(404, "Empty list.")
        return self.render_to_response(await invoke(self.get_context_data, **kwargs))
