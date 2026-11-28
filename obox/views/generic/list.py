from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import Request
from fastapi.responses import Response
from starlette.datastructures import URL

from obox.components.query import parameters
from obox.core.async_utils import invoke
from obox.core.exceptions import ImproperlyConfigured, NotFound, PermissionDenied
from obox.core.pagination import Page
from obox.resources import ModelResource
from obox.views.generic._model import ModelContextMixin, ModelT, ModelTemplateMixin, field_options
from obox.views.generic._query import ViewConfig
from obox.views.generic.base import View
from obox.web.i18n import language_url


class GridView(ModelTemplateMixin, ModelContextMixin[ModelT], View):
    fields: Sequence[str] | None = None
    page_size: int | None = None
    view = ViewConfig()
    object_list: Sequence[ModelT]

    def check(self) -> None:
        super().check()
        if self.fields is not None and (
            not isinstance(self.fields, Sequence)
            or isinstance(self.fields, str)
            or any(not isinstance(name, str) for name in self.fields)
        ):
            raise ImproperlyConfigured("GridView.fields must be a sequence of field names.")
        if self.page_size is not None and (
            type(self.page_size) is not int or not 1 <= self.page_size <= 100
        ):
            raise ImproperlyConfigured("Choose a page size between 1 and 100.")
        if not isinstance(self.view, ViewConfig):
            raise ImproperlyConfigured("Provide a ViewConfig for view.")

    def _collection_url(self, url: URL | None = None, **updates: int) -> str:
        params = {**self.request.query_params, **updates}
        target = self.request.url if url is None else url
        return str(target.include_query_params(**params))

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
        return self.model_template_names(self.bound_resource().model, "grid", suffix="_grid")

    async def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        resource = self.bound_resource()
        params = parameters(
            request.query_params.multi_items(), {"page", "q", "filter", "ordering", "group"}
        )
        state = resource.collection(
            params,
            default_filter=self.view.filter,
            default_sort=self.view.sort,
            default_group=self.view.group,
        )
        try:
            number = int(params.get("page", "1"))
        except ValueError as exc:
            raise NotFound("Invalid page.") from exc
        size = self.page_size or min(resource.page_size or 50, 100)
        page = await resource.paginate(number, page_size=size, collection=state)
        self.object_list = page.items
        fields = field_options(resource)
        selected = resource.readable if self.fields is None else self.fields
        columns = tuple(name for name in dict.fromkeys(selected) if name in fields)
        groups = tuple(term["field"] for term in state.group)
        counts = await resource.group_counts(state, page)
        rows = await self._rows(resource, page, columns, groups, counts)
        context = {
            **kwargs,
            "title": resource.model.__name__,
            "object_list": self.object_list,
            "page": page,
            "columns": [
                {**fields[name], "primary": index == 0} for index, name in enumerate(columns)
            ],
            "query": {
                "filter": state.filter,
                "sort": state.sort,
                "group": state.group,
                "search": state.search,
                "searchable": bool(resource.query_fields["search"]),
                "fields": [fields[name] for name in resource.query_fields["filter"]],
                "ordering_fields": [fields[name] for name in resource.query_fields["ordering"]],
                "group_fields": [fields[name] for name in resource.query_fields["group"]],
            },
            "rows": rows,
            "total": page.total,
            "add_url": await self.write_url("create", resource),
            "language_url": lambda code: self._collection_url(URL(language_url(request, code))),
            "previous_url": self._collection_url(page=number - 1) if page.has_previous else None,
            "next_url": self._collection_url(page=number + 1) if page.has_next else None,
        }
        return self.render_to_response(await invoke(self.get_context_data, **context))
