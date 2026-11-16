"""The supported ModelAdmin contract, exposed as ModelBuilder."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from obox.db import _session
from obox.forms import ModelForm, modelform_factory
from obox.views.generic.detail import _model_label


def _flatten(fields: Any) -> list[str]:
    return [
        name
        for item in fields
        for name in (_flatten(item) if isinstance(item, (list, tuple)) else [item])
    ]


class ModelBuilder:
    form: type[ModelForm] = ModelForm
    fields: Any = None
    fieldsets: Any = None
    exclude: Any = None
    readonly_fields: Sequence[str] = ()
    list_display: Sequence[Any] = ("__str__",)
    list_display_links: Any = ()
    list_filter: Sequence[str] = ()
    search_fields: Sequence[str] = ()
    ordering: Sequence[str] = ()
    list_per_page = 100
    actions: Any = ()
    change_list_template: str | None = None
    add_form_template: str | None = None
    change_form_template: str | None = None
    delete_confirmation_template: str | None = None

    def __init__(self, model: type[Any], admin_site: Any) -> None:
        self.model, self.admin_site = model, admin_site
        self.app_label, self.model_name = _model_label(model)

    def get_queryset(self, request: Any) -> Any:
        query = sa.select(self.model)
        ordering = self.get_ordering(request)
        if ordering:
            query = query.order_by(
                *(
                    getattr(self.model, name.lstrip("-")).desc()
                    if name.startswith("-")
                    else getattr(self.model, name)
                    for name in ordering
                )
            )
        return query

    def get_ordering(self, request: Any) -> Any:
        return self.ordering

    def get_list_display(self, request: Any) -> Any:
        return self.list_display

    def get_list_display_links(self, request: Any, list_display: Any) -> Any:
        if self.list_display_links is None or self.list_display_links:
            return self.list_display_links
        return list_display[:1]

    def get_list_filter(self, request: Any) -> Any:
        return self.list_filter

    def get_search_fields(self, request: Any) -> Any:
        return self.search_fields

    def get_readonly_fields(self, request: Any, obj: Any = None) -> Any:
        return self.readonly_fields

    def get_fields(self, request: Any, obj: Any = None) -> Any:
        if self.fields is not None:
            return self.fields
        if self.fieldsets is not None:
            return _flatten([options["fields"] for _, options in self.fieldsets])
        meta = getattr(self.form, "Meta", None)
        defines_fields = (
            getattr(meta, "fields", None) is not None or getattr(meta, "exclude", None) is not None
        )
        cls = modelform_factory(
            self.model,
            self.form,
            fields=None if defines_fields else "__all__",
            exclude=[
                *(self.get_exclude(request, obj) or getattr(meta, "exclude", ()) or ()),
                *self.get_readonly_fields(request, obj),
            ],
        )
        return [*cls.base_fields, *self.get_readonly_fields(request, obj)]

    def get_fieldsets(self, request: Any, obj: Any = None) -> Any:
        return self.fieldsets or [(None, {"fields": self.get_fields(request, obj)})]

    def get_exclude(self, request: Any, obj: Any = None) -> Any:
        return self.exclude

    def get_form(
        self, request: Any, obj: Any = None, change: bool = False, **kwargs: Any
    ) -> type[ModelForm]:
        fields = _flatten(kwargs.pop("fields", self.get_fields(request, obj)))
        readonly = tuple(self.get_readonly_fields(request, obj))
        excluded = self.get_exclude(request, obj)
        exclude = [
            *(
                excluded
                if excluded is not None
                else (getattr(getattr(self.form, "Meta", None), "exclude", ()) or ())
            ),
            *readonly,
        ]
        if change and hasattr(request, "user") and not self.has_change_permission(request, obj):
            exclude.extend(fields)
        form = kwargs.pop("form", self.form)
        form = type(
            form.__name__,
            (form,),
            dict.fromkeys(name for name in readonly if name in form.declared_fields),
        )
        return modelform_factory(
            self.model,
            form,
            fields=[name for name in fields if name not in exclude],
            exclude=exclude,
            **kwargs,
        )

    def get_search_results(self, request: Any, queryset: Any, search_term: str) -> tuple[Any, bool]:
        for word in search_term.split():
            predicates = []
            for name in self.get_search_fields(request):
                prefix = name[:1]
                column = getattr(self.model, name[1:] if prefix in ("^", "=") else name)
                predicates.append(
                    column.istartswith(word, autoescape=True)
                    if prefix == "^"
                    else sa.func.lower(column) == word.lower()
                    if prefix == "="
                    else column.icontains(word, autoescape=True)
                )
            if predicates:
                queryset = queryset.where(sa.or_(*predicates))
        return queryset, False

    async def save_model(self, request: Any, obj: Any, form: ModelForm, change: bool) -> None:
        async with _session() as session:
            session.add(obj)
            await session.flush()

    async def save_related(
        self, request: Any, form: ModelForm, formsets: Any, change: bool
    ) -> None:
        pass

    async def delete_model(self, request: Any, obj: Any) -> None:
        async with _session() as session:
            await session.delete(obj)
            await session.flush()

    async def delete_queryset(self, request: Any, queryset: Any) -> None:
        async with _session() as session:
            for obj in (await session.scalars(queryset)).unique():
                await session.delete(obj)
            await session.flush()

    def _permission(self, request: Any, action: str) -> bool:
        return bool(request.user.has_perm(f"{self.app_label}.{action}_{self.model_name}"))

    def has_add_permission(self, request: Any) -> bool:
        return self._permission(request, "add")

    def has_change_permission(self, request: Any, obj: Any = None) -> bool:
        return self._permission(request, "change")

    def has_delete_permission(self, request: Any, obj: Any = None) -> bool:
        return self._permission(request, "delete")

    def has_view_permission(self, request: Any, obj: Any = None) -> bool:
        return self._permission(request, "view") or self._permission(request, "change")

    def get_model_perms(self, request: Any) -> dict[str, bool]:
        return {
            name: bool(getattr(self, f"has_{name}_permission")(request))
            for name in ("add", "change", "delete", "view")
        }

    def get_actions(self, request: Any) -> dict[str, Any]:
        if self.actions is None:
            return {}
        result = dict(self.admin_site._actions)
        for action in self.actions:
            method = getattr(type(self), action) if isinstance(action, str) else action
            result[method.__name__] = method
        allowed = {}
        for name, method in result.items():
            permissions = getattr(method, "allowed_permissions", ("change",))
            if any(
                getattr(self, f"has_{permission}_permission")(request) for permission in permissions
            ):
                allowed[name] = (
                    method,
                    name,
                    getattr(method, "short_description", name.replace("_", " ").capitalize()),
                )
        return allowed
