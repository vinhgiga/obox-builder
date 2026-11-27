from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar, Generic

import sqlalchemy as sa
from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import URL

from obox.core.async_utils import invoke
from obox.core.exceptions import ImproperlyConfigured, NotFound, PermissionDenied
from obox.core.pagination import Page
from obox.db.query import group_query
from obox.db.session import session_scope
from obox.resources import ModelResource
from obox.views.generic._model import (
    ModelContextMixin,
    ModelT,
    ModelTemplateMixin,
    field_options,
)
from obox.views.generic._query import QUERY_PARAMETERS, ViewConfig, ViewQuery
from obox.views.generic.base import TemplateView, View
from obox.views.generic.decorators import action
from obox.web.http import read_form
from obox.web.i18n import language_url


class ListView(ModelTemplateMixin, ModelContextMixin[ModelT], View, Generic[ModelT]):
    fields: Sequence[str] | None = None
    search_fields: Sequence[str] = ()
    page_size: int | None = None
    view = ViewConfig()
    actions: Sequence[Any] | None = ()
    editable: Sequence[str] = ()
    slug = "list"
    collection_routes: ClassVar[tuple[tuple[str, str, str], ...]] = ()
    object_list: Sequence[ModelT]

    _template = "list"

    def check(self) -> None:
        super().check()
        if "template_name_suffix" in vars(self) or any(
            "template_name_suffix" in vars(cls)
            for cls in type(self).__mro__
            if issubclass(cls, ListView)
        ):
            raise ImproperlyConfigured("Use template_name instead of template_name_suffix.")
        if self.fields is not None and (
            not isinstance(self.fields, Sequence)
            or isinstance(self.fields, str)
            or any(not isinstance(name, str) for name in self.fields)
        ):
            raise ImproperlyConfigured("ListView.fields must be a sequence of field names.")
        if (
            not isinstance(self.search_fields, Sequence)
            or isinstance(self.search_fields, str)
            or any(not isinstance(name, str) for name in self.search_fields)
        ):
            raise ImproperlyConfigured("ListView.search_fields must be a sequence of field names.")
        if self.page_size is not None and (
            type(self.page_size) is not int or not 1 <= self.page_size <= 100
        ):
            raise ImproperlyConfigured("Choose a page size between 1 and 100.")
        if not isinstance(self.view, ViewConfig):
            raise ImproperlyConfigured("Provide a ViewConfig for view.")

    def get_queryset(self) -> sa.Select[tuple[ModelT]]:
        return self.bound_resource().get_queryset()

    async def _resolve_collection(self) -> tuple[ModelResource[ModelT], Any, ViewQuery]:
        queryset = await invoke(self.get_queryset)
        resource = self.bound_resource()
        model = resource.model
        allowed = {
            name
            for name in resource.readable
            if not isinstance(resource.specs[name].column.type, sa.JSON)
        }
        state = ViewQuery.parse(
            model,
            self.request.query_params,
            allowed,
            defaults=self.view,
            search_fields=self.search_fields,
        )
        return resource, state.apply(queryset.order_by(*resource.get_ordering()), model), state

    def get_actions(self, model: type[ModelT]) -> dict[str, tuple[Any, str, str]]:
        allowed = {}
        for declaration in self.actions or ():
            method = (
                getattr(type(self), declaration) if isinstance(declaration, str) else declaration
            )
            name = method.__name__
            permissions = getattr(method, "allowed_permissions", ("change",))
            if any(self.has_model_permission(model, permission) for permission in permissions):
                label = getattr(method, "short_description", name.replace("_", " ").capitalize())
                allowed[name] = (method, name, label)
        return allowed

    def _collection_url(self, url: URL | None = None, **updates: int) -> str:
        params = self.request.query_params
        has_filter = bool(params.get("filter"))
        current = {
            key: value for key, value in params.items() if not has_filter or key in QUERY_PARAMETERS
        }
        return str((url or self.request.url).replace_query_params(**{**current, **updates}))

    @action(permissions=("delete",), description="Delete selected objects")
    async def delete_selected(self, request: Request, queryset: Any) -> Response | None:
        resource = self.bound_resource()
        data = await read_form(request)
        if data.get("post") != "yes":
            view = TemplateView(template_name="obox/generic/delete_selected.html")
            view.setup(request)
            return view.render_to_response(
                {"selected": data.getlist("_selected_action"), "action": "delete_selected"}
            )
        async with session_scope() as session:
            bound = resource.bind(session, request.user)
            for obj in (await bound.list(queryset=queryset.with_for_update())).items:
                await bound.destroy(obj)
        return None

    async def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        if not self.actions:
            return self.http_method_not_allowed(request, *args, **kwargs)
        data = await read_form(request)
        resource, queryset, _ = await self._resolve_collection()
        if selected_action := self.get_actions(resource.model).get(str(data.get("action", ""))):
            try:
                selected = [
                    resource.pk.type.python_type(value)
                    for value in data.getlist("_selected_action")
                ]
            except (TypeError, ValueError) as exc:
                raise PermissionDenied("Invalid selection.") from exc
            result = await invoke(
                selected_action[0],
                self,
                request,
                resource._query(queryset).where(resource.pk.in_(selected)),
            )
            if isinstance(result, Response):
                return result
        return RedirectResponse(self._collection_url(), status_code=303)

    def allowed_methods(self) -> list[str]:
        methods = super().allowed_methods()
        if not self.actions and type(self).post is ListView.post and "POST" in methods:
            methods.remove("POST")
        return methods

    async def _group_counts(
        self,
        resource: ModelResource[ModelT],
        queryset: Any,
        page: Page[ModelT],
        names: tuple[str, ...],
    ) -> dict[tuple[Any, ...], int]:
        counts = {}
        for depth in range(1, len(names) + 1):
            prefix = names[:depth]
            keys = {tuple(getattr(obj, name) for name in prefix) for obj in page}
            if not keys:
                break
            columns = [getattr(resource.model, name) for name in prefix]
            statement = group_query(queryset, resource.pk, columns, order="key.asc").where(
                sa.or_(
                    *(
                        sa.and_(
                            *(column == value for column, value in zip(columns, key, strict=True))
                        )
                        for key in keys
                    )
                )
            )
            for *key, count in await self.session.execute(statement):
                counts[tuple(key)] = count
        return counts

    async def _rows(
        self,
        resource: ModelResource[ModelT],
        page: Page[ModelT],
        columns: Sequence[str],
        groups: tuple[str, ...],
        counts: dict[tuple[Any, ...], int],
    ) -> list[dict[str, Any]]:
        rows = []
        previous: tuple[Any, ...] = ()
        for obj in page:
            url = None
            if "detail" in self.route_names and self.has_model_permission(resource.model, "view"):
                try:
                    await resource.check_permission("retrieve", obj)
                except (NotFound, PermissionDenied):
                    pass
                else:
                    url = self.route_url("detail", pk=str(getattr(obj, resource.lookup_name)))
            keys = tuple(getattr(obj, name) for name in groups)
            headers = []
            for index, name in enumerate(groups, 1):
                prefix = keys[:index]
                if prefix != previous[:index]:
                    headers.append(
                        {
                            "depth": index,
                            "count": counts.get(prefix, 1),
                            "label": name.replace("_", " ").capitalize(),
                            "value": resource.specs[name].serialize(keys[index - 1]),
                        }
                    )
            previous = keys
            rows.append(
                {
                    "pk": getattr(obj, resource.pk_name),
                    "values": [
                        resource.specs[name].serialize(getattr(obj, name)) for name in columns
                    ],
                    "url": url,
                    "groups": headers,
                }
            )
        return rows

    def get_template_names(self) -> list[str]:
        if self.template_name:
            return [self.template_name]
        return self.model_template_names(
            self._list_model, self._template, suffix=f"_{self._template}"
        )

    async def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        resource, queryset, state = await self._resolve_collection()
        self._list_model = model = resource.model
        try:
            number = int(request.query_params.get("page", "1"))
        except ValueError as exc:
            raise NotFound("Invalid page.") from exc
        size = self.page_size or min(resource.page_size or 50, 100)
        async with session_scope() as session:
            bound = resource.bind(session, request.user)
            page = await bound.paginate(number, page_size=size, queryset=queryset)
            self.object_list = page.items
            columns = tuple(
                dict.fromkeys(
                    name
                    for name in (resource.readable if self.fields is None else self.fields)
                    if name in resource.readable
                )
            )
            fields = field_options(resource)
            groups = tuple(term["field"] for term in state.group)
            counts = (
                await self._group_counts(bound, bound._query(queryset), page, groups)
                if groups
                else {}
            )
            rows = await self._rows(bound, page, columns, groups, counts)
            add_url = await self.write_url("create", bound)

        context = {
            **kwargs,
            "title": model.__name__,
            "object_list": self.object_list,
            "page": page,
            "collection_kind": self._template,
            "columns": [
                {**fields[name], "primary": index == 0} for index, name in enumerate(columns)
            ],
            "query": {
                "filter": state.filter,
                "sort": state.sort,
                "group": state.group,
                "search": state.search,
                "searchable": bool(state.search_fields),
                "fields": [
                    field
                    for name, field in fields.items()
                    if not isinstance(resource.specs[name].column.type, sa.JSON)
                ],
            },
            "layout": {
                "hidden": [name for name in self.view.hidden if name in columns[1:]],
                "order": [name for name in self.view.order if name in columns[1:]],
                "row_height": self.view.row_height,
            },
            "rows": rows,
            "total": page.total,
            "actions": self.get_actions(model),
            "add_url": add_url,
            "language_url": lambda code: self._collection_url(URL(language_url(request, code))),
            "previous_url": self._collection_url(page=number - 1) if page.has_previous else None,
            "next_url": self._collection_url(page=number + 1) if page.has_next else None,
            "view_links": [
                {"label": label, "url": self.reverse(name), "active": slug == self.slug}
                for slug, label, name in self.collection_routes
            ],
        }
        return self.render_to_response(await invoke(self.get_context_data, **context))


class GridView(ListView[ModelT]):
    slug = "grid"
    _template = "grid"
