from __future__ import annotations

from typing import Any, cast
from urllib.parse import urlencode

import sqlalchemy as sa
from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from starlette.exceptions import HTTPException

from obox._async import invoke
from obox.builder.decorators import action
from obox.builder.options import ModelBuilder, _flatten
from obox.conf import settings
from obox.core.exceptions import PermissionDenied
from obox.urls import path
from obox.views.generic import CreateView, DeleteView, ListView, TemplateView, UpdateView
from obox.views.generic.base import _identify


class AlreadyRegistered(Exception):
    pass


class NotRegistered(Exception):
    pass


@action(permissions=("delete",), description="Delete selected objects")
async def delete_selected(modeladmin: ModelBuilder, request: Request, queryset: Any) -> Any:
    if request.state.admin_data.get("post") != "yes":
        view = TemplateView(template_name="obox/admin/delete_selected.html")
        view.setup(request)
        return view.render_to_response(
            {
                "selected": request.state.admin_data.getlist("_selected_action"),
                "action": "delete_selected",
            }
        )
    await invoke(modeladmin.delete_queryset, request, queryset)
    return None


class AdminSite:
    site_title = "Obox site admin"
    site_header = "Obox administration"
    index_title = "Site administration"

    def __init__(self, name: str = "admin") -> None:
        self.name = name
        self._registry: dict[type[Any], ModelBuilder] = {}
        self._actions: dict[str, Any] = {"delete_selected": delete_selected}

    def register(
        self, model_or_iterable: Any, admin_class: type[ModelBuilder] | None = None, **options: Any
    ) -> None:
        models = [model_or_iterable] if isinstance(model_or_iterable, type) else model_or_iterable
        cls = admin_class or ModelBuilder
        if options:
            cls = type(cls.__name__, (cls,), options)
        for model in models:
            if model in self._registry:
                raise AlreadyRegistered(f"The model {model.__name__} is already registered.")
            self._registry[model] = cls(model, self)

    def unregister(self, model_or_iterable: Any) -> None:
        models = [model_or_iterable] if isinstance(model_or_iterable, type) else model_or_iterable
        for model in models:
            if model not in self._registry:
                raise NotRegistered(f"The model {model.__name__} is not registered.")
            del self._registry[model]

    def is_registered(self, model: type[Any]) -> bool:
        return model in self._registry

    def has_permission(self, request: Request) -> bool:
        return bool(
            getattr(request.user, "is_active", False) and getattr(request.user, "is_staff", False)
        )

    def add_action(self, action: Any, name: str | None = None) -> None:
        self._actions[name or action.__name__] = action

    def admin_view(self, view: Any, cacheable: bool = False) -> Any:
        async def protected(request: Request) -> Response:
            await _identify(request)
            if not self.has_permission(request):
                runtime = getattr(request.app.state, "obox", None)
                login = (
                    str(request.url_for(runtime.auth.login_route))
                    if runtime is not None and runtime.auth
                    else settings.LOGIN_URL
                )
                return RedirectResponse(
                    login
                    + "?"
                    + urlencode(
                        {
                            "next": request.url.path
                            + ("?" + request.url.query if request.url.query else "")
                        }
                    ),
                    status_code=302,
                )
            response = await view(request)
            if not cacheable:
                response.headers["Cache-Control"] = (
                    "max-age=0, no-cache, no-store, must-revalidate, private"
                )
            from obox.middleware.csrf import _set_cookie

            _set_cookie(request, response)
            return cast(Response, response)

        endpoint: Any = protected
        endpoint.http_method_names = getattr(view, "http_method_names", ("get",))
        return endpoint

    async def index(self, request: Request) -> Response:
        view = TemplateView(template_name="obox/admin/index.html")
        view.setup(request)
        models = []
        for model, admin in self._registry.items():
            if await invoke(admin.has_view_permission, request):
                models.append(
                    {"name": model.__name__, "url": f"{admin.app_label}/{admin.model_name}/"}
                )
        return view.render_to_response({"title": self.index_title, "models": models})

    def get_urls(self) -> list[Any]:
        patterns = [path("", self.admin_view(self.index), name="index")]
        for model, owner in self._registry.items():
            patterns.extend(self._model_urls(model, owner))
        return patterns

    @property
    def urls(self) -> Any:
        return self.get_urls(), "admin", self.name

    def _model_urls(self, model: type[Any], owner: ModelBuilder) -> list[Any]:
        prefix = f"{owner.app_label}/{owner.model_name}/"
        basename = f"{owner.app_label}_{owner.model_name}"
        site = self

        class AdminMixin:
            model: Any
            request: Request
            object: Any
            operation = "view"

            async def dispatch(self, request: Request, *args: Any, **kwargs: Any) -> Any:
                if not await invoke(getattr(owner, f"has_{self.operation}_permission"), request):
                    raise PermissionDenied()
                return await invoke(cast(Any, super()).dispatch, request, *args, **kwargs)

            async def get_queryset(self) -> Any:
                return await invoke(owner.get_queryset, self.request)

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

        class AdminFormMixin(AdminMixin):
            def get_form_class(self) -> Any:
                return owner.get_form(self.request, self.object, change=self.object is not None)

            def get_success_url(self) -> str:
                return str(self.request.url_for(f"{site.name}:{basename}_changelist"))

            async def form_valid(self, form: Any) -> Response:
                self.object = await form.save(commit=False)
                change = self.operation != "add"
                if change and not await invoke(
                    owner.has_change_permission, self.request, self.object
                ):
                    raise PermissionDenied()
                await invoke(owner.save_model, self.request, self.object, form, change)
                await invoke(owner.save_related, self.request, form, [], change)
                return RedirectResponse(self.get_success_url(), status_code=302)

            async def get_context_data(self, **kwargs: Any) -> Any:
                context = await invoke(cast(Any, super()).get_context_data, **kwargs)
                can_change = self.object is None or await invoke(
                    owner.has_change_permission, self.request, self.object
                )
                readonly = (
                    owner.get_readonly_fields(self.request, self.object)
                    if can_change
                    else _flatten(owner.get_fields(self.request, self.object))
                )
                groups = [
                    {
                        "title": title,
                        "description": options.get("description", ""),
                        "fields": _flatten(options["fields"]),
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
                                f"{site.name}:{basename}_delete",
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
                        else None,
                        "readonly_values": {
                            name: getattr(self.object, name, "") if self.object is not None else ""
                            for name in readonly
                        },
                    }
                )
                return context

        class Add(AdminFormMixin, CreateView):
            operation = "add"
            template_name = (
                owner.add_form_template or owner.change_form_template or "obox/admin/form.html"
            )

        class Change(AdminFormMixin, UpdateView):
            template_name = owner.change_form_template or "obox/admin/form.html"

        class Delete(AdminMixin, DeleteView):
            operation = "delete"
            template_name = owner.delete_confirmation_template or "obox/generic/confirm_delete.html"

            def get_success_url(self) -> str:
                return str(self.request.url_for(f"{site.name}:{basename}_changelist"))

            async def form_valid(self, form: Any) -> Response:
                await invoke(owner.delete_model, self.request, self.object)
                return RedirectResponse(self.get_success_url(), status_code=302)

        class ChangeList(AdminMixin, ListView):
            template_name = owner.change_list_template or "obox/admin/change_list.html"
            paginate_by = owner.list_per_page
            page_kwarg = "p"

            async def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
                try:
                    kwargs["p"] = int(request.query_params.get("p", "0")) + 1
                except ValueError as exc:
                    raise HTTPException(404, "Invalid page.") from exc
                return await super().get(request, *args, **kwargs)

            async def get_queryset(self) -> Any:
                queryset = await super().get_queryset()
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

            async def get_context_data(self, **kwargs: Any) -> Any:
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
                                self.request.url_for(
                                    f"{site.name}:{basename}_change", pk=str(getattr(obj, pk))
                                )
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
                        "add_url": str(self.request.url_for(f"{site.name}:{basename}_add"))
                        if await invoke(owner.has_add_permission, self.request)
                        else None,
                    }
                )
                return context

            async def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
                from obox._http import read_form

                data = await read_form(request)
                action = owner.get_actions(request).get(str(data.get("action", "")))
                if action is None:
                    return RedirectResponse(str(request.url), status_code=302)
                queryset = await self.get_queryset()
                column = sa.inspect(model).primary_key[0]
                try:
                    selected = [
                        column.type.python_type(value) for value in data.getlist("_selected_action")
                    ]
                except (ValueError, TypeError) as exc:
                    raise PermissionDenied("Invalid selection.") from exc
                queryset = queryset.where(column.in_(selected))
                request.state.admin_data = data
                result = await invoke(action[0], owner, request, queryset)
                return (
                    result
                    if isinstance(result, Response)
                    else RedirectResponse(str(request.url), status_code=302)
                )

        patterns = []
        for suffix, cls, name in (
            ("", ChangeList, "changelist"),
            ("add/", Add, "add"),
            ("<str:pk>/change/", Change, "change"),
            ("<str:pk>/delete/", Delete, "delete"),
        ):
            cls.model = model
            patterns.append(
                path(prefix + suffix, self.admin_view(cls.as_view()), name=f"{basename}_{name}")
            )
        return patterns


site = AdminSite()
