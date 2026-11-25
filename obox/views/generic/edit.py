from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from typing import Any, ClassVar, cast

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel

from obox.core.async_utils import invoke
from obox.core.exceptions import (
    BuilderValidationError,
    ImproperlyConfigured,
    PermissionDenied,
    ResourceConflict,
    RuleViolation,
)
from obox.db.session import session_scope
from obox.db.transaction import savepoint_scope
from obox.forms import Form, ModelForm, modelform_factory
from obox.resources import WriteAction, resolve_resource
from obox.views.generic.base import ContextMixin, TemplateResponseMixin, View
from obox.views.generic.detail import SingleObjectMixin, SingleObjectTemplateResponseMixin
from obox.views.generic.inlines import Inline, prepare_inlines
from obox.web.http import form_input


def _flatten(fields: Any) -> list[str]:
    return [
        name
        for item in fields
        for name in (_flatten(item) if isinstance(item, (list, tuple)) else [item])
    ]


class FormMixin(ContextMixin):
    initial: ClassVar[dict[str, Any]] = {}
    form_class: Any = None
    schema: type[BaseModel] | None = None
    invalid_status_code = 200
    success_url: str | None = None
    prefix: str | None = None
    request: Request
    render_to_response: Any
    reverse: Any

    def check(self) -> None:
        super().check()
        if self.schema is not None and (
            not isinstance(self.schema, type) or not issubclass(self.schema, BaseModel)
        ):
            raise ImproperlyConfigured("Define a Pydantic form schema.")
        if self.schema is None and self.form_class is None and not isinstance(self, ModelFormMixin):
            raise ImproperlyConfigured("Define form_class or schema.")
        if (
            not self.success_url
            and not getattr(self, "route_names", {}).get("list")
            and type(self).get_success_url is FormMixin.get_success_url
            and type(self).form_valid is FormMixin.form_valid
        ):
            raise ImproperlyConfigured("No URL to redirect to. Provide a success_url.")

    def get_initial(self) -> dict[str, Any]:
        # Class-level defaults may contain nested, mutable values. Never share
        # them between requests or with a form that normalizes its input.
        return deepcopy(self.initial)

    def get_prefix(self) -> str | None:
        return self.prefix

    def get_form_class(self) -> Any:
        return self.form_class or (Form if self.schema else None)

    async def get_form_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"initial": self.get_initial(), "prefix": self.get_prefix()}
        if self.request.method in ("POST", "PUT", "PATCH"):
            kwargs.update(await form_input(self.request))
        if self.schema is not None:
            kwargs["schema"] = self.schema
        return kwargs

    async def get_form(self, form_class: Any = None) -> Any:
        cls = form_class or await invoke(self.get_form_class)
        if cls is None:
            raise ImproperlyConfigured("Define form_class.")
        form = cls(**await invoke(self.get_form_kwargs))
        await form.prepare()
        return form

    def get_success_url(self) -> str:
        if not self.success_url:
            raise ImproperlyConfigured("No URL to redirect to. Provide a success_url.")
        return (
            self.success_url
            if self.success_url.startswith(("/", "http://", "https://"))
            else str(self.reverse(self.success_url))
        )

    async def form_valid(self, form: Form[Any]) -> Response:
        return RedirectResponse(await invoke(self.get_success_url), status_code=303)

    async def form_invalid(self, form: Form[Any]) -> Response:
        return cast(
            Response,
            self.render_to_response(
                await invoke(self.get_context_data, form=form), status_code=self.invalid_status_code
            ),
        )

    async def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        if "form" not in kwargs:
            kwargs["form"] = await self.get_form()
        return cast(dict[str, Any], await invoke(super().get_context_data, **kwargs))


class FormView(TemplateResponseMixin, FormMixin, View):
    async def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return self.render_to_response(await invoke(self.get_context_data))

    async def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        form = await self.get_form()
        return cast(
            Response,
            (
                await invoke(self.form_valid, form)
                if await form.is_valid()
                else await invoke(self.form_invalid, form)
            ),
        )

    async def put(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return await self.post(request, *args, **kwargs)


class ModelFormMixin(FormMixin, SingleObjectMixin):
    def check(self) -> None:
        super().check()
        form_class = self.get_form_class()
        resource = self.bound_resource()
        model = (
            resource.model
            if resource is not None
            else self.model or getattr(getattr(form_class, "Meta", None), "model", None)
        )
        if (
            not self.success_url
            and not self.route_names.get("list")
            and not hasattr(model, "get_absolute_url")
            and type(self).get_success_url is ModelFormMixin.get_success_url
            and type(self).form_valid is ModelFormMixin.form_valid
        ):
            raise ImproperlyConfigured("No URL to redirect to. Provide a success_url.")

    fields: Any = None
    fieldsets: Sequence[tuple[str | None, dict[str, Any]]] = ()
    inlines: Sequence[type[Inline]] = ()

    def get_form_class(self) -> Any:
        if self.fields is not None and self.form_class is not None:
            raise ImproperlyConfigured("Specifying both fields and form_class is not permitted.")
        if self.form_class is not None:
            return cast(type[ModelForm[Any]], self.form_class)
        if resource := self.bound_resource():
            return modelform_factory(
                resource.model,
                fields=self.fields
                or tuple(
                    resource.get_schema("create" if self.object is None else "update").model_fields
                ),
                schema=self.schema
                or resource.get_schema("create" if self.object is None else "update"),
            )
        model = self.model or type(self.object)
        if self.fields is None:
            raise ImproperlyConfigured("Define fields or form_class.")
        return modelform_factory(model, fields=self.fields)

    async def get_form_kwargs(self) -> dict[str, Any]:
        return {
            **await super().get_form_kwargs(),
            "instance": self.object,
            "resource": self.bound_resource(),
        }

    async def get_form(self, form_class: Any = None) -> Any:
        form = await super().get_form(form_class)
        if resource := self.bound_resource():
            action: WriteAction = "create" if self.object is None else "update"
            await resource.check_permission(action, self.object)
            allowed = await resource.writable_fields(action, self.object)
            submitted = {
                name
                for name in resource.specs
                if form.add_prefix(name) in form.data or form.add_prefix(name) in form.files
            }
            if denied := submitted - allowed:
                raise PermissionDenied(f"Fields cannot be written: {', '.join(sorted(denied))}.")
            for name, field in tuple(form.fields.items()):
                if name in resource.specs and name not in resource.readable:
                    del form.fields[name]
                    continue
                if name in resource.specs and name not in allowed:
                    field.disabled = True
        return form

    async def _save_resource_form(self, form: ModelForm[Any]) -> Any:
        return await form.save()

    async def form_valid(self, form: Any) -> Response:
        groups = await self.get_inline_formsets(form)
        if not all([await group["formset"].is_valid() for group in groups]):
            return cast(Response, await invoke(self.form_invalid, form))
        change = self.object is not None
        try:
            async with session_scope() as session, savepoint_scope(session):
                self.object = await self._save_resource_form(form)
                await invoke(self.save_model, self.request, self.object, form, change)
                formsets = [group["formset"] for group in groups]
                for formset in formsets:
                    formset.instance = self.object
                await invoke(self.save_related, self.request, form, formsets, change)
        except BuilderValidationError as exc:
            await self.restore_object()
            return await self._render_resource_errors(form, exc)
        except (RuleViolation, ResourceConflict) as exc:
            await self.restore_object()
            form.add_error(None, str(exc))
            return await self.form_invalid(form)
        return await super().form_valid(form)

    def get_inlines(self, request: Request, obj: Any = None) -> Sequence[type[Inline]]:
        return self.inlines

    async def get_inline_formsets(self, form: ModelForm[Any]) -> list[dict[str, Any]]:
        if not hasattr(self, "_inline_formsets"):
            declarations = await invoke(self.get_inlines, self.request, self.object)
            self._inline_formsets = await prepare_inlines(
                declarations, type(form.instance), self.request, self.object, form
            )
        return self._inline_formsets

    async def save_model(
        self, request: Request, obj: Any, form: ModelForm[Any], change: bool
    ) -> None:
        """Run additional view behavior after saving the record."""

    async def save_related(
        self, request: Request, form: ModelForm[Any], formsets: Sequence[Any], change: bool
    ) -> None:
        for formset in formsets:
            await invoke(self.save_formset, request, form, formset, change)

    async def save_formset(
        self, request: Request, form: ModelForm[Any], formset: Any, change: bool
    ) -> None:
        await formset.save()

    async def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = await super().get_context_data(**kwargs)
        form = context["form"]
        resource = self.bound_resource()
        model = type(form.instance)
        visible = resource.readable if resource is not None else form.fields
        readonly = {
            name: getattr(self.object, name, "") if self.object is not None else ""
            for name in visible
            if name not in form.fields or form.fields[name].disabled
        }
        delete_url = None
        if (
            self.object is not None
            and resource is not None
            and "delete" in self.route_names
            and self.has_model_permission(model, "delete")
            and await invoke(resource.can, "delete", self.object, self.request.user)
        ):
            delete_url = self.route_url(
                "delete", pk=str(getattr(self.object, resource.lookup_name))
            )
        return {
            "title": model.__name__,
            "fieldsets": [
                {
                    "title": title,
                    "description": options.get("description"),
                    "fields": [name for name in _flatten(options["fields"]) if name in visible],
                }
                for title, options in self.fieldsets
            ],
            "readonly_fields": readonly.keys(),
            "readonly_values": readonly,
            "inline_formsets": await self.get_inline_formsets(form),
            "delete_url": delete_url,
            **context,
        }

    async def _render_resource_errors(
        self, form: ModelForm[Any], exc: BuilderValidationError
    ) -> Response:
        async with session_scope() as session:
            if not session.is_active:
                raise exc
        for field, messages in exc.errors.items():
            for message in messages:
                form.add_error(
                    field.split(".")[0] if field.split(".")[0] in form.fields else None, message
                )
        return await self.form_invalid(form)

    def get_success_url(self) -> str:
        if self.success_url:
            target = self.success_url.format(**vars(self.object))
            return (
                target
                if target.startswith(("/", "http://", "https://"))
                else str(self.reverse(target))
            )
        if url := self.route_url("list"):
            return url
        if hasattr(self.object, "get_absolute_url"):
            return str(self.object.get_absolute_url())
        raise ImproperlyConfigured("No URL to redirect to. Provide a success_url.")


class CreateView(SingleObjectTemplateResponseMixin, ModelFormMixin, FormView):
    action = "create"
    template_name_suffix = "_form"

    async def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        self.object = None
        return await super().get(request, *args, **kwargs)

    async def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        self.object = None
        return await super().post(request, *args, **kwargs)


class UpdateView(SingleObjectTemplateResponseMixin, ModelFormMixin, FormView):
    action = "update"
    template_name_suffix = "_form"

    async def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        self.object = await self.get_object()
        return await super().get(request, *args, **kwargs)

    async def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        self.object = await self.get_object()
        return await super().post(request, *args, **kwargs)


class DeleteView(SingleObjectTemplateResponseMixin, SingleObjectMixin, FormMixin, View):
    action = "destroy"
    template_name_suffix = "_confirm_delete"
    form_class = Form

    def check(self) -> None:
        super().check()
        if (
            not self.success_url
            and not self.route_names.get("list")
            and type(self).get_success_url is DeleteView.get_success_url
            and type(self).form_valid is DeleteView.form_valid
        ):
            raise ImproperlyConfigured("No URL to redirect to. Provide a success_url.")

    async def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        self.object = await self.get_object()
        if resource := self.bound_resource():
            await resource.check_permission("destroy", self.object)
        return self.render_to_response(await invoke(self.get_context_data, **kwargs))

    async def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        self.object = await self.get_object()
        form = await self.get_form()
        return cast(
            Response,
            await invoke(self.form_valid if await form.is_valid() else self.form_invalid, form),
        )

    def get_success_url(self) -> str:
        if self.success_url:
            target = self.success_url.format(**vars(self.object))
            return (
                target
                if target.startswith(("/", "http://", "https://"))
                else str(self.reverse(target))
            )
        if url := self.route_url("list"):
            return url
        raise ImproperlyConfigured("No URL to redirect to. Provide a success_url.")

    async def form_valid(self, form: Form[Any]) -> Response:
        url = self.get_success_url()
        try:
            async with session_scope() as session, savepoint_scope(session):
                resource = self.bound_resource() or resolve_resource(type(self.object))
                await resource.bind(session, self.request.user).destroy(self.object)
        except (BuilderValidationError, RuleViolation, ResourceConflict) as exc:
            await self.restore_object()
            errors = (
                [message for messages in exc.errors.values() for message in messages]
                if isinstance(exc, BuilderValidationError)
                else [str(exc)]
            )
            form.add_error(None, errors)
            return await self.form_invalid(form)
        return RedirectResponse(url, status_code=303)

    async def delete(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        self.object = await self.get_object()
        return await self.form_valid(Form({}))
