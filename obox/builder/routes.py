from __future__ import annotations

from typing import Any, cast

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from starlette.exceptions import HTTPException

from obox._async import invoke
from obox._dependencies import permission_required
from obox._exceptions import BuilderValidationError
from obox._page_routes import PageRoute
from obox.builder.decorators import action
from obox.builder.options import ModelBuilder, _flatten
from obox.core.exceptions import PermissionDenied
from obox.db import _session
from obox.resources import Resource
from obox.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    TemplateView,
    UpdateView,
)


@action(permissions=("delete",), description="Delete selected objects")
async def delete_selected(builder: ModelBuilder, request: Request, queryset: Any) -> Any:
    if request.state.builder_data.get("post") != "yes":
        view = TemplateView(template_name="obox/builder/delete_selected.html")
        view.setup(request)
        return view.render_to_response(
            {
                "selected": request.state.builder_data.getlist("_selected_action"),
                "action": "delete_selected",
            }
        )
    resource = builder.resource
    if resource is not None:
        async with _session() as session:
            for obj in await resource._rows(session, queryset.with_for_update()):
                await resource._delete(session, request.user, obj)
    return None


def build_views(prefix: str, resource: Resource[Any], owner: ModelBuilder) -> APIRouter:
    model = resource.model
    basename = f"obox:views:{prefix}"
    router = APIRouter(prefix=prefix, include_in_schema=False, route_class=PageRoute)

    class BuilderMixin:
        model: Any
        request: Request
        object: Any
        operation = "view"

        async def dispatch(self, request: Request, *args: Any, **kwargs: Any) -> Any:
            if not await invoke(getattr(owner, f"has_{self.operation}_permission"), request):
                raise PermissionDenied()
            return await invoke(cast(Any, super()).dispatch, request, *args, **kwargs)

        async def get_queryset(self) -> Any:
            return await invoke(resource._query, self.request.user)

        async def get_object(self, queryset: Any = None) -> Any:
            obj = await invoke(cast(Any, super()).get_object, queryset)
            permission = (
                owner.has_change_permission
                if self.request.method == "POST" and self.operation == "view"
                else getattr(owner, f"has_{self.operation}_permission")
            )
            if not await invoke(permission, self.request, obj):
                raise PermissionDenied()
            return obj

    class BuilderFormMixin(BuilderMixin):
        inline_formsets: list[dict[str, Any]]

        async def get_inline_formsets(self, form: Any) -> Any:
            if hasattr(self, "inline_formsets"):
                return self.inline_formsets
            groups: list[dict[str, Any]] = []
            prefixes: dict[str, int] = {}
            declarations = await invoke(owner.get_formsets_with_inlines, self.request, self.object)
            for cls, inline in declarations:
                permissions = {
                    action: await invoke(
                        getattr(inline, f"has_{action}_permission"),
                        self.request,
                        *([] if action == "add" else [self.object]),
                    )
                    for action in ("view", "add", "change", "delete")
                }
                if not any(permissions.values()):
                    continue
                prefix = cls.get_default_prefix()
                prefixes[prefix] = prefixes.get(prefix, 0) + 1
                if prefixes[prefix] > 1:
                    prefix += f"-{prefixes[prefix]}"
                queryset = await invoke(inline.get_queryset, self.request)
                if not any(permissions[key] for key in ("view", "change", "delete")):
                    queryset = queryset.where(sa.false())
                formset = cls(
                    data=form.data if form.is_bound else None,
                    files=form.files if form.is_bound else None,
                    instance=form.instance,
                    prefix=prefix,
                    queryset=queryset,
                )
                if not permissions["add"]:
                    formset.extra, formset.max_num = 0, 0
                formset.can_delete = inline.can_delete and permissions["delete"]
                await formset.prepare()
                readonly = inline.get_readonly_fields(self.request, self.object)
                for child in formset:
                    for name, field in child.fields.items():
                        if name in readonly or (
                            child in formset.initial_forms
                            and not permissions["change"]
                            and name != "DELETE"
                        ):
                            field.disabled = True
                if form.is_bound:
                    await formset.is_valid()
                    if not permissions["add"] and any(
                        child.has_changed() and not formset._should_delete_form(child)
                        for child in formset.extra_forms
                    ):
                        raise PermissionDenied()
                groups.append({"formset": formset, "inline": inline})
            self.inline_formsets = groups
            return groups

        def get_form_class(self) -> Any:
            return owner.get_form(self.request, self.object, change=self.object is not None)

        def get_success_url(self) -> str:
            return str(self.request.url_for(f"{basename}:changelist"))

        async def form_valid(self, form: Any) -> Response:
            groups = await self.get_inline_formsets(form)
            if not all([await group["formset"].is_valid() for group in groups]):
                return cast(Response, await invoke(cast(Any, self).form_invalid, form))
            change = self.operation != "add"
            if change and not await invoke(owner.has_change_permission, self.request, self.object):
                raise PermissionDenied()

            try:
                self.object = await cast(Any, self)._save_resource_form(form)
            except BuilderValidationError as exc:
                return cast(Response, await cast(Any, self)._render_resource_errors(form, exc))
            await invoke(owner.save_model, self.request, self.object, form, change)
            for group in groups:
                group["formset"].instance = self.object
            await invoke(
                owner.save_related,
                self.request,
                form,
                [group["formset"] for group in groups],
                change,
            )
            return RedirectResponse(self.get_success_url(), status_code=303)

        async def get_context_data(self, **kwargs: Any) -> Any:
            context = await invoke(cast(Any, super()).get_context_data, **kwargs)
            context["inline_formsets"] = await self.get_inline_formsets(context["form"])
            can_change = self.object is None or await invoke(
                owner.has_change_permission, self.request, self.object
            )
            form = context["form"]
            readonly = tuple(
                name
                for name in resource.readable
                if name not in form.fields or form.fields[name].disabled
            )
            groups = [
                {
                    "title": title,
                    "description": options.get("description", ""),
                    "fields": [
                        name for name in _flatten(options["fields"]) if name in resource.readable
                    ],
                }
                for title, options in owner.get_fieldsets(self.request, self.object)
            ]
            context.update(
                {
                    "title": model.__name__,
                    "fieldsets": groups,
                    "readonly_fields": readonly,
                    "has_change_permission": can_change,
                    "delete_url": str(
                        self.request.url_for(
                            f"{basename}:delete",
                            pk=str(
                                getattr(
                                    self.object,
                                    sa.inspect(model)
                                    .get_property_by_column(sa.inspect(model).primary_key[0])
                                    .key,
                                )
                            ),
                        )
                    )
                    if self.object is not None
                    and await invoke(owner.has_delete_permission, self.request, self.object)
                    and await invoke(resource.can, "delete", self.object, self.request.user)
                    else None,
                    "readonly_values": {
                        name: getattr(self.object, name, "") if self.object is not None else ""
                        for name in readonly
                    },
                }
            )
            return context

    class Add(BuilderFormMixin, CreateView):
        operation = "add"
        template_name = (
            owner.add_form_template or owner.change_form_template or "obox/builder/form.html"
        )

    class Change(BuilderFormMixin, UpdateView):
        operation = "change"
        template_name = owner.change_form_template or "obox/builder/form.html"

    class Delete(BuilderMixin, DeleteView):
        operation = "delete"
        template_name = owner.delete_confirmation_template or "obox/generic/confirm_delete.html"

        def get_success_url(self) -> str:
            return str(self.request.url_for(f"{basename}:changelist"))

    class ChangeList(BuilderMixin, ListView):
        template_name = owner.change_list_template or "obox/builder/change_list.html"
        paginate_by = owner.list_per_page
        page_kwarg = "p"
        add_url: str | None = None

        async def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
            try:
                kwargs["p"] = int(request.query_params.get("p", "0")) + 1
            except ValueError as exc:
                raise HTTPException(404, "Invalid page.") from exc
            if await invoke(owner.has_add_permission, request) and await invoke(
                resource.can, "create", None, request.user
            ):
                self.add_url = str(request.url_for(f"{basename}:add"))
            return await super().get(request, *args, **kwargs)

        async def get_queryset(self) -> Any:
            queryset = await super().get_queryset()
            queryset = queryset.order_by(None).order_by(*resource._order())
            for name in owner.get_list_filter(self.request):
                raw = self.request.query_params.get(
                    name + "__exact", self.request.query_params.get(name)
                )
                if raw is not None:
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
                    queryset = queryset.where(column == value)
            queryset, _ = await invoke(
                owner.get_search_results,
                self.request,
                queryset,
                self.request.query_params.get("q", ""),
            )
            return queryset

        def get_context_data(self, **kwargs: Any) -> Any:
            context = cast(Any, super()).get_context_data(**kwargs)
            columns = owner.get_list_display(self.request)
            rows = []
            links = owner.get_list_display_links(self.request, columns) or ()
            pk = sa.inspect(model).get_property_by_column(sa.inspect(model).primary_key[0]).key
            for obj in self.object_list:
                row = []
                for name in columns:
                    value = (
                        name(obj)
                        if callable(name)
                        else getattr(owner, name)(obj)
                        if callable(getattr(owner, name, None)) and name != "__str__"
                        else getattr(obj, name)
                    )
                    row.append(value() if callable(value) else value)
                rows.append(
                    {
                        "pk": getattr(obj, pk),
                        "values": row,
                        "url": str(
                            self.request.url_for(f"{basename}:detail", pk=str(getattr(obj, pk)))
                        ),
                    }
                )
            context.update(
                {
                    "title": model.__name__,
                    "list_display": columns,
                    "rows": rows,
                    "list_display_links": links,
                    "actions": owner.get_actions(self.request),
                    "search_fields": owner.get_search_fields(self.request),
                    "search_term": self.request.query_params.get("q", ""),
                    "filters": [
                        (name, self.request.query_params.get(name + "__exact", ""))
                        for name in owner.get_list_filter(self.request)
                    ],
                    "previous_url": str(
                        self.request.url.include_query_params(p=self._page.number - 2)
                    )
                    if self._page and self._page.has_previous()
                    else None,
                    "next_url": str(self.request.url.include_query_params(p=self._page.number))
                    if self._page and self._page.has_next()
                    else None,
                    "add_url": self.add_url,
                }
            )
            return context

        async def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
            from obox._http import read_form

            data = await read_form(request)
            action = owner.get_actions(request).get(str(data.get("action", "")))
            if action is None:
                return RedirectResponse(str(request.url), status_code=303)
            queryset = await self.get_queryset()
            column = sa.inspect(model).primary_key[0]
            try:
                selected = [
                    column.type.python_type(value) for value in data.getlist("_selected_action")
                ]
            except (ValueError, TypeError) as exc:
                raise PermissionDenied("Invalid selection.") from exc
            queryset = queryset.where(column.in_(selected))
            request.state.builder_data = data
            result = await invoke(action[0], owner, request, queryset)
            return (
                result
                if isinstance(result, Response)
                else RedirectResponse(str(request.url), status_code=303)
            )

    class Detail(BuilderMixin, DetailView):
        template_name = "obox/builder/detail.html"

        change_url: str | None = None

        async def get_object(self, queryset: Any = None) -> Any:
            obj = await super().get_object(queryset)
            if await invoke(owner.has_change_permission, self.request, obj) and await invoke(
                resource.can, "update", obj, self.request.user
            ):
                self.change_url = str(
                    self.request.url_for(
                        f"{basename}:change", pk=str(getattr(obj, resource.pk_name))
                    )
                )
            return obj

        def get_context_data(self, **kwargs: Any) -> Any:
            context = super().get_context_data(**kwargs)
            context.update(
                {
                    "title": model.__name__,
                    "values": [
                        (name, resource.specs[name].serialize(getattr(self.object, name)))
                        for name in resource.readable
                    ],
                    "change_url": self.change_url,
                }
            )
            return context

    model_label = f"{owner.app_label}." + "{action}_" + owner.model_name
    for suffix, cls, base, custom, name, methods, operation in (
        ("", ChangeList, ListView, owner.list_view, "changelist", ["GET", "HEAD", "POST"], "view"),
        ("/add", Add, CreateView, owner.create_view, "add", ["GET", "HEAD", "POST"], "add"),
        ("/{pk}/view", Detail, DetailView, owner.detail_view, "detail", ["GET", "HEAD"], "view"),
        (
            "/{pk}/change",
            Change,
            UpdateView,
            owner.update_view,
            "change",
            ["GET", "HEAD", "POST"],
            "change",
        ),
        (
            "/{pk}/delete",
            Delete,
            DeleteView,
            owner.delete_view,
            "delete",
            ["GET", "HEAD", "POST"],
            "delete",
        ),
    ):
        view = cls if custom is base else type(cls.__name__, (custom, cls), {})
        endpoint = cast(Any, view).as_view(resource=resource)
        router.add_api_route(
            suffix,
            endpoint,
            methods=methods,
            name=f"{basename}:{name}",
            dependencies=[
                Depends(permission_required(model_label.format(action=operation), html=True))
            ],
            response_model=None,
            include_in_schema=False,
        )
    return router
