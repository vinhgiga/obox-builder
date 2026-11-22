from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import sqlalchemy as sa
from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from starlette.exceptions import HTTPException

from obox._async import invoke
from obox._http import read_form
from obox.core.exceptions import ImproperlyConfigured, PermissionDenied
from obox.db import _session
from obox.db._identity import _model_label
from obox.views.generic._model import ModelContextMixin
from obox.views.generic.base import TemplateResponseMixin, TemplateView, View
from obox.views.generic.decorators import action


class _Page:
    def __init__(self, objects: Any, number: int, count: int, per_page: int) -> None:
        self.object_list, self.number = objects, number
        from types import SimpleNamespace

        self.paginator = SimpleNamespace(
            count=count, per_page=per_page, num_pages=max(1, (count + per_page - 1) // per_page)
        )

    def has_next(self) -> bool:
        return bool(self.number < self.paginator.num_pages)

    def has_previous(self) -> bool:
        return self.number > 1

    def next_page_number(self) -> int:
        if not self.has_next():
            raise ValueError("That page contains no results")
        return self.number + 1

    def previous_page_number(self) -> int:
        if not self.has_previous():
            raise ValueError("That page number is less than 1")
        return self.number - 1


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

    def get_ordering(self) -> Any:
        return self.ordering

    def get_queryset(self) -> Any:
        if resource := self._bound_resource():
            query = resource._query(self.request.user).order_by(*resource._order())
        elif self.queryset is not None:
            query = self.queryset._generate()
        elif self.model is not None:
            query = sa.select(self.model)
        else:
            raise ImproperlyConfigured("Define model, queryset, or get_queryset().")
        model = query.column_descriptions[0]["entity"]
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
        for name in self.get_list_filter():
            raw = self.request.query_params.get(
                name + "__exact", self.request.query_params.get(name)
            )
            if raw is None:
                continue
            column = getattr(model, name)
            try:
                if isinstance(column.type, sa.Boolean):
                    if raw not in ("0", "1", "True", "False"):
                        raise ValueError
                    value = raw in ("1", "True")
                else:
                    value = column.type.python_type(raw)
            except (TypeError, ValueError) as exc:
                raise PermissionDenied("Invalid filter.") from exc
            query = query.where(column == value)
        if self.search_fields:
            query = self.get_search_results(query, self.request.query_params.get("q", ""))
        return query

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
        for word in term.split():
            predicates = []
            for name in self.search_fields:
                prefix = name[:1]
                field = name[1:] if prefix in ("^", "=") else name
                if resource is not None and field not in resource.readable:
                    continue
                column = getattr(model, field)
                predicates.append(
                    column.istartswith(word, autoescape=True)
                    if prefix == "^"
                    else sa.func.lower(column) == word.lower()
                    if prefix == "="
                    else column.icontains(word, autoescape=True)
                )
            if predicates:
                queryset = queryset.where(sa.or_(*predicates))
        return queryset

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
            for obj in await resource._rows(session, queryset.with_for_update()):
                await resource._delete(session, request.user, obj)
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
        page = self._page
        context = {
            "object_list": self.object_list,
            self.context_object_name or model.__name__.lower() + "_list": self.object_list,
            "is_paginated": page is not None and page.paginator.num_pages > 1,
            "page_obj": page,
            "paginator": page.paginator if page is not None else None,
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
            "previous_url": self._page_url(page.previous_page_number())
            if page and page.has_previous()
            else None,
            "next_url": self._page_url(page.next_page_number())
            if page and page.has_next()
            else None,
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
        count, number = 0, 1
        resource = self._bound_resource()
        if (
            "create" in self.route_names
            and self._has_model_permission(self._list_model, "add")
            and (resource is None or await invoke(resource.can, "create", None, request.user))
        ):
            self.add_url = self._route_url("create")
        offset = 0
        async with _session() as session:
            if self.paginate_by:
                count = (
                    await resource._count(session, query)
                    if resource
                    else int(
                        await session.scalar(
                            sa.select(sa.func.count()).select_from(query.order_by(None).subquery())
                        )
                        or 0
                    )
                )
                raw = kwargs.get(
                    self.page_kwarg, request.query_params.get(self.page_kwarg, str(self.page_start))
                )
                try:
                    number = (
                        max(1, (count + self.paginate_by - 1) // self.paginate_by)
                        if raw == "last" and self.page_start == 1
                        else int(raw) - self.page_start + 1
                    )
                except ValueError as exc:
                    raise HTTPException(404, "Invalid page.") from exc
                if number < 1 or number > max(
                    1, (count + self.paginate_by - 1) // self.paginate_by
                ):
                    raise HTTPException(404, "Invalid page.")
                offset = (number - 1) * self.paginate_by
            if resource:
                self.object_list = await resource._rows(
                    session, query, offset=offset, limit=self.paginate_by
                )
            else:
                if self.paginate_by:
                    query = query.offset(offset).limit(self.paginate_by)
                self.object_list = list((await session.scalars(query)).unique())
            if self.paginate_by:
                self._page = _Page(self.object_list, number, count, self.paginate_by)
        if not self.allow_empty and not self.object_list:
            raise HTTPException(404, "Empty list.")
        return self.render_to_response(await invoke(self.get_context_data, **kwargs))
