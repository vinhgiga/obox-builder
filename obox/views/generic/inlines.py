from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from obox.core.async_utils import invoke
from obox.core.exceptions import PermissionDenied
from obox.core.principal import has_permission
from obox.forms import BaseInlineFormSet, ModelForm, inlineformset_factory
from obox.views.generic._model import model_permission


class Inline:
    model: type[Any]
    fk_name: str | None = None
    formset: Any = None
    extra = 3
    min_num = 0
    max_num: int | None = None
    can_delete = True
    template = "obox/generic/stacked.html"
    form: type[ModelForm[Any]] = ModelForm
    fields: Any = None
    exclude: Any = None
    readonly_fields: Sequence[str] = ()
    ordering: Sequence[str] = ()

    def __init__(self, parent_model: type[Any]) -> None:
        self.parent_model = parent_model

    def get_queryset(self, request: Any) -> Any:
        return sa.select(self.model).order_by(
            *(
                getattr(self.model, name.removeprefix("-")).desc()
                if name.startswith("-")
                else getattr(self.model, name)
                for name in self.ordering
            )
        )

    def get_fields(self, request: Any, obj: Any = None) -> Any:
        if self.fields is not None:
            return self.fields
        meta = getattr(self.form, "Meta", None)
        if getattr(meta, "fields", None) is not None or getattr(meta, "exclude", None) is not None:
            return None
        return "__all__"

    def get_exclude(self, request: Any, obj: Any = None) -> Any:
        return self.exclude

    def get_readonly_fields(self, request: Any, obj: Any = None) -> Sequence[str]:
        return self.readonly_fields

    def _permission(self, request: Any, operation: str) -> bool:
        return has_permission(request.user, model_permission(self.model, operation))

    def has_add_permission(self, request: Any) -> bool:
        return self._permission(request, "add")

    def has_change_permission(self, request: Any, obj: Any = None) -> bool:
        return self._permission(request, "change")

    def has_view_permission(self, request: Any, obj: Any = None) -> bool:
        return self._permission(request, "view")

    def has_delete_permission(self, request: Any, obj: Any = None) -> bool:
        return self._permission(request, "delete")

    def get_extra(self, request: Any, obj: Any = None, **kwargs: Any) -> int:
        return self.extra

    def get_min_num(self, request: Any, obj: Any = None, **kwargs: Any) -> int:
        return self.min_num

    def get_max_num(self, request: Any, obj: Any = None, **kwargs: Any) -> int | None:
        return self.max_num

    def get_formset(self, request: Any, obj: Any = None, **kwargs: Any) -> Any:
        inline = self
        base: Any = self.formset or BaseInlineFormSet

        class PermissionFormSet(base):  # type: ignore[misc]
            async def save_new(self, form: Any, commit: bool = True) -> Any:
                if not await invoke(inline.has_add_permission, request):
                    raise PermissionDenied()
                return await super().save_new(form, commit)

            async def save_existing(self, form: Any, instance: Any, commit: bool = True) -> Any:
                if not await invoke(inline.has_change_permission, request, instance):
                    raise PermissionDenied()
                return await super().save_existing(form, instance, commit)

            async def delete_existing(self, instance: Any, commit: bool = True) -> None:
                if not await invoke(inline.has_delete_permission, request, instance):
                    raise PermissionDenied()
                await super().delete_existing(instance, commit)

        return inlineformset_factory(
            self.parent_model,
            self.model,
            self.form,
            formset=PermissionFormSet,
            fk_name=self.fk_name,
            fields=self.get_fields(request, obj),
            exclude=self.get_exclude(request, obj),
            extra=self.get_extra(request, obj),
            min_num=self.get_min_num(request, obj),
            max_num=self.get_max_num(request, obj),
            can_delete=self.can_delete,
            **kwargs,
        )


class TabularInline(Inline):
    template = "obox/generic/tabular.html"


class StackedInline(Inline):
    pass


async def prepare_inlines(
    declarations: Sequence[type[Inline]],
    parent_model: type[Any],
    request: Any,
    obj: Any,
    form: ModelForm[Any],
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    prefixes: dict[str, int] = {}
    for declaration in declarations:
        inline = declaration(parent_model)
        cls = inline.get_formset(request, obj)
        permissions = {
            action: await invoke(
                getattr(inline, f"has_{action}_permission"),
                request,
                *([] if action == "add" else [obj]),
            )
            for action in ("view", "add", "change", "delete")
        }
        if not any(permissions.values()):
            continue
        prefix = cls.get_default_prefix()
        prefixes[prefix] = prefixes.get(prefix, 0) + 1
        if prefixes[prefix] > 1:
            prefix += f"-{prefixes[prefix]}"
        queryset = await invoke(inline.get_queryset, request)
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
        readonly = inline.get_readonly_fields(request, obj)
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
    return groups
