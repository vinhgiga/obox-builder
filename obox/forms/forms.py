"""Bound forms with declarative fields and asynchronous validation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from html import escape
from typing import Any, ClassVar

from markupsafe import Markup

from obox._async import invoke
from obox.core.exceptions import NON_FIELD_ERRORS, ValidationError
from obox.forms.fields import ChoiceField, Field, JSONField


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
        return (
            self.form.initial.get(self.name, self.field.initial)
            if not self.form.is_bound or self.field.disabled
            else self.form.data.get(self.html_name)
        )

    def __html__(self) -> str:
        attrs: dict[str, Any] = {"id": self.id_for_label}
        if self.field.required:
            attrs["required"] = True
        if self.field.disabled:
            attrs["disabled"] = True
        if isinstance(self.field, ChoiceField):
            self.field.widget.choices = self.field.choices
        value = self.value()
        if isinstance(self.field, JSONField) and (
            not self.form.is_bound or self.field.disabled or not isinstance(value, str)
        ):
            value = json.dumps(value, ensure_ascii=False)
        return str(self.field.widget.render(self.html_name, value, attrs))

    def __str__(self) -> str:
        return self.__html__()


class Form:
    base_fields: ClassVar[dict[str, Field]] = {}
    declared_fields: ClassVar[dict[str, Field]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        fields: dict[str, Field] = {}
        for parent in reversed(cls.__mro__[1:]):
            fields.update(getattr(parent, "declared_fields", {}))
        for name, value in list(vars(cls).items()):
            if isinstance(value, Field):
                fields[name] = value
                delattr(cls, name)
            elif value is None:
                fields.pop(name, None)
        cls.declared_fields = fields
        cls.base_fields = fields

    def __init__(
        self,
        data: Mapping[str, Any] | None = None,
        files: Mapping[str, Any] | None = None,
        *,
        initial: Mapping[str, Any] | None = None,
        prefix: str | None = None,
    ) -> None:
        self.is_bound = data is not None or files is not None
        self.data, self.files = data if data is not None else {}, files or {}
        self.initial, self.prefix = dict(initial or {}), prefix
        self.fields = deepcopy(self.base_fields)
        self.cleaned_data: dict[str, Any] = {}
        self.errors: dict[str, list[str]] = {}
        self._error_codes: dict[str, list[str | None]] = {}
        self._validated = False

    def add_prefix(self, field_name: str) -> str:
        return f"{self.prefix}-{field_name}" if self.prefix else field_name

    def __getitem__(self, name: str) -> BoundField:
        return BoundField(self, self.fields[name], name)

    def __iter__(self) -> Any:
        return (self[name] for name in self.fields)

    async def is_valid(self) -> bool:
        if not self._validated:
            await self.full_clean()
        return self.is_bound and not self.errors

    async def full_clean(self) -> None:
        self._validated = True
        self.errors, self.cleaned_data, self._error_codes = {}, {}, {}
        if not self.is_bound:
            return
        for name, field in self.fields.items():
            value = (
                self.initial.get(name, field.initial)
                if field.disabled
                else self.data.get(self.add_prefix(name))
            )
            try:
                self.cleaned_data[name] = await invoke(field.clean, value)
                method = getattr(self, f"clean_{name}", None)
                if method is not None:
                    self.cleaned_data[name] = await invoke(method)
            except ValidationError as exc:
                self.add_error(name, exc)
        try:
            cleaned = await invoke(self.clean)
            if cleaned is not None:
                self.cleaned_data = cleaned
        except ValidationError as exc:
            self.add_error(None, exc)
        await self._post_clean()

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
            self.errors.setdefault(name, []).extend(messages)
            self._error_codes.setdefault(name, []).append(error.code)
            self.cleaned_data.pop(name, None)

    def non_field_errors(self) -> list[str]:
        return self.errors.get(NON_FIELD_ERRORS, [])

    def has_error(self, field: str, code: str | None = None) -> bool:
        return field in self.errors and (code is None or code in self._error_codes.get(field, ()))

    def as_p(self) -> Markup:
        result = "".join(
            f'<p><label for="{escape(bound.id_for_label)}">{escape(bound.label)}:</label> '
            f"{bound.__html__()}"
            + "".join(f'<span class="error">{escape(message)}</span>' for message in bound.errors)
            + (
                f'<span class="helptext">{escape(bound.field.help_text)}</span>'
                if bound.field.help_text
                else ""
            )
            + "</p>"
            for bound in self
        )
        return Markup(result)  # noqa: S704 -- every value is escaped above.
