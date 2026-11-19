"""Bound forms with declarative fields and asynchronous validation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from copy import deepcopy
from enum import Enum
from html import escape
from typing import Any, ClassVar

from markupsafe import Markup
from starlette.datastructures import UploadFile

from obox._async import invoke
from obox.core.exceptions import NON_FIELD_ERRORS, ValidationError
from obox.forms.declarative import MISSING, TypedField, collect_fields
from obox.forms.fields import ChoiceField, Field, JSONField
from obox.forms.widgets import FileInput


class BoundField:
    def __init__(self, form: Form, field: Field, name: str) -> None:
        self.form, self.field, self.name = form, field, name
        self.label = field.label or name.replace("_", " ").capitalize()
        self.html_name = form.add_prefix(name)
        self.id_for_label = f"id_{self.html_name}"

    @property
    def errors(self) -> list[str]:
        return self.form.errors.get(self.name, [])

    def value(self) -> Any:
        value = (
            self.form._initial_value(self.name, self.field)
            if not self.form.is_bound or self.field.disabled
            else self.form._data_value(self.name, self.field)
        )
        if value is MISSING:
            return None
        prepare = getattr(self.field, "prepare_value", None)
        return prepare(value) if prepare is not None else value

    def __html__(self) -> str:
        attrs: dict[str, Any] = {"id": self.id_for_label}
        if self.field.required and self.form.use_required_attribute:
            attrs["required"] = True
        if self.field.disabled:
            attrs["disabled"] = True
        if isinstance(self.field, ChoiceField):
            self.field.widget.choices = self.field.choices
        value = self.value()
        if (isinstance(self.field, JSONField) or getattr(self.field, "is_json", False)) and (
            not self.form.is_bound or self.field.disabled or not isinstance(value, str)
        ):
            value = json.dumps(value, ensure_ascii=False)
        if isinstance(value, Enum):
            value = value.value
        return str(self.field.widget.render(self.html_name, value, attrs))

    def __str__(self) -> str:
        return self.__html__()

    @property
    def is_hidden(self) -> bool:
        return bool(self.field.widget.input_type == "hidden")


class Form:
    base_fields: ClassVar[dict[str, Field]] = {}
    declared_fields: ClassVar[dict[str, Field]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        fields = cls._collect_fields()
        cls.declared_fields = fields
        cls.base_fields = fields

    @classmethod
    def _collect_fields(cls) -> dict[str, Field]:
        reserved = set(vars(Form)) | {
            "is_bound",
            "data",
            "files",
            "initial",
            "prefix",
            "fields",
            "cleaned_data",
            "errors",
            "empty_permitted",
            "use_required_attribute",
        }
        return collect_fields(cls, Form, reserved)

    def __init__(
        self,
        data: Mapping[str, Any] | None = None,
        files: Mapping[str, Any] | None = None,
        *,
        initial: Mapping[str, Any] | None = None,
        prefix: str | None = None,
        empty_permitted: bool = False,
    ) -> None:
        self.is_bound = data is not None or files is not None
        self.data, self.files = data if data is not None else {}, files or {}
        self.initial, self.prefix = dict(initial or {}), prefix
        self.empty_permitted = empty_permitted
        self.use_required_attribute = True
        self.fields = deepcopy(self.base_fields)
        self.cleaned_data: dict[str, Any] = {}
        self.errors: dict[str, list[str]] = {}
        self._error_codes: dict[str, list[str | None]] = {}
        self._validated = False
        self._validation_lock = asyncio.Lock()
        self._defaults: dict[str, Any] = {}
        self._values: dict[str, Any] | None = None

    def _default_value(self, name: str, field: TypedField) -> Any:
        if not field.has_default:
            return MISSING
        if name not in self._defaults:
            self._defaults[name] = field.get_default()
        return self._defaults[name]

    def _initial_value(self, name: str, field: Field) -> Any:
        if name in self.initial:
            return self.initial[name]
        if isinstance(field, TypedField) and field.initial is MISSING:
            value = self._default_value(name, field)
            return None if value is MISSING else value
        return field.initial

    def add_prefix(self, field_name: str) -> str:
        return f"{self.prefix}-{field_name}" if self.prefix else field_name

    def __getitem__(self, name: str) -> BoundField:
        return BoundField(self, self.fields[name], name)

    def __iter__(self) -> Any:
        return (self[name] for name in self.fields)

    def _data_value(self, name: str, field: Field) -> Any:
        key = self.add_prefix(name)
        if isinstance(field.widget, FileInput):
            return self.files.get(key)
        getlist = getattr(self.data, "getlist", None)
        if getattr(field.widget, "allow_multiple_selected", False) and getlist is not None:
            return getlist(key)
        return self.data.get(key, MISSING if isinstance(field, TypedField) else None)

    def is_multipart(self) -> bool:
        return any(
            getattr(field.widget, "needs_multipart_form", False) for field in self.fields.values()
        )

    async def prepare(self) -> None:
        """Load asynchronous choices before rendering a form."""
        for field in self.fields.values():
            prepare = getattr(field, "prepare", None)
            if prepare is not None:
                await invoke(prepare)

    @property
    def changed_data(self) -> list[str]:
        changed = []
        for name, field in self.fields.items():
            if field.disabled:
                continue
            initial, value = self._initial_value(name, field), self._data_value(name, field)
            has_changed = getattr(field, "has_changed", None)
            if has_changed is not None:
                if has_changed(initial, value):
                    changed.append(name)
                continue
            prepare = getattr(field, "prepare_value", None)
            if prepare is not None:
                initial, value = prepare(initial), prepare(value)
            try:
                converter = field.converter if isinstance(field, TypedField) else field
                if prepare is None:
                    initial = converter.to_python(None if initial is MISSING else initial)
                    value = converter.to_python(None if value is MISSING else value)
                if isinstance(initial, (list, tuple)) and isinstance(value, (list, tuple)):
                    different = {str(item) for item in initial} != {str(item) for item in value}
                else:
                    different = ("" if initial in field.empty_values else str(initial)) != (
                        "" if value in field.empty_values else str(value)
                    )
            except (ValidationError, TypeError, ValueError):
                different = True
            if different:
                changed.append(name)
        return changed

    def has_changed(self) -> bool:
        return bool(self.changed_data)

    async def is_valid(self) -> bool:
        async with self._validation_lock:
            if not self._validated:
                await self.full_clean()
        return self.is_bound and not self.errors

    async def full_clean(self) -> None:
        await self._full_clean()

    async def _full_clean(self) -> None:
        self._validated, self._values = False, None
        self.errors, self.cleaned_data, self._error_codes = {}, {}, {}
        if not self.is_bound or (self.empty_permitted and not self.has_changed()):
            self._validated = True
            return
        for name, field in self.fields.items():
            value = (
                self._initial_value(name, field)
                if field.disabled
                else self._data_value(name, field)
            )
            try:
                if value is MISSING and isinstance(field, TypedField):
                    value = self._default_value(name, field)
                self.cleaned_data[name] = await invoke(field.clean, value)
                method = getattr(self, f"clean_{name}", None)
                if method is not None:
                    self.cleaned_data[name] = await invoke(
                        method,
                        *([self.cleaned_data[name]] if isinstance(field, TypedField) else []),
                    )
                if isinstance(field, TypedField):
                    self.cleaned_data[name] = field.validate(self.cleaned_data[name], strict=True)
            except ValidationError as exc:
                self.add_error(name, exc)
        try:
            cleaned = await invoke(self.clean)
            if cleaned is not None:
                self.cleaned_data = cleaned
        except ValidationError as exc:
            self.add_error(None, exc)
        for name, field in self.fields.items():
            if name in self.errors:
                self.cleaned_data.pop(name, None)
            elif isinstance(field, TypedField):
                try:
                    self.cleaned_data[name] = field.validate(
                        self.cleaned_data.get(name, MISSING), strict=True
                    )
                except ValidationError as exc:
                    self.add_error(name, exc)
        await self._post_clean()
        if not self.errors:
            values = {
                name: value
                for name, value in self.cleaned_data.items()
                if isinstance(self.fields.get(name), TypedField)
            }
            # Uploaded streams have a request-owned lifetime and cannot be copied.
            uploads = {
                id(value): value for value in values.values() if isinstance(value, UploadFile)
            }
            self._values = deepcopy(values, uploads)
        self._validated = True

    async def _post_clean(self) -> None:
        pass

    def clean(self) -> dict[str, Any]:
        return self.cleaned_data

    def add_error(self, field: str | None, error: Any) -> None:
        error = error if isinstance(error, ValidationError) else ValidationError(error)
        errors = (
            error.message_dict
            if hasattr(error, "message_dict")
            else {field or NON_FIELD_ERRORS: error.messages}
        )
        for name, messages in errors.items():
            if name != NON_FIELD_ERRORS and name not in self.fields:
                raise ValueError(f"{type(self).__name__!r} has no field named {name!r}.")
            self._values = None
            self.errors.setdefault(name, []).extend(messages)
            self._error_codes.setdefault(name, []).append(error.code)
            self.cleaned_data.pop(name, None)

    def non_field_errors(self) -> list[str]:
        return self.errors.get(NON_FIELD_ERRORS, [])

    def has_error(self, field: str, code: str | None = None) -> bool:
        return field in self.errors and (code is None or code in self._error_codes.get(field, ()))

    def as_p(self) -> Markup:
        result = "".join(
            (
                ""
                if bound.is_hidden
                else f'<p><label for="{escape(bound.id_for_label)}">{escape(bound.label)}:</label> '
            )
            + bound.__html__()
            + "".join(f'<span class="error">{escape(message)}</span>' for message in bound.errors)
            + (
                f'<span class="helptext">{escape(bound.field.help_text)}</span>'
                if bound.field.help_text
                else ""
            )
            + ("" if bound.is_hidden else "</p>")
            for bound in self
        )
        return Markup(result)  # noqa: S704 -- every value is escaped above.
