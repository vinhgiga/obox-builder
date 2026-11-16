from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from obox import forms
from obox._async import invoke
from obox.conf import settings
from obox.core.exceptions import ValidationError as FormError
from obox.rest_framework.exceptions import ValidationError


class empty:  # noqa: N801 - DRF sentinel name.
    pass


def _representation(value: Any) -> Any:
    if isinstance(value, datetime):
        if settings.USE_TZ:
            value = value.replace(tzinfo=UTC) if value.tzinfo is None else value
            value = value.astimezone(ZoneInfo(settings.TIME_ZONE))
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value


class Field:
    def __init__(
        self,
        *,
        read_only: bool = False,
        write_only: bool = False,
        required: bool | None = None,
        default: Any = empty,
        allow_null: bool = False,
        source: str | None = None,
        validators: Any = (),
        error_messages: dict[str, str] | None = None,
    ) -> None:
        if read_only and write_only:
            raise AssertionError("May not set both read_only and write_only.")
        if required and (read_only or default is not empty):
            raise AssertionError("required cannot be combined with read_only or default.")
        self.validators = list(validators)
        self.error_messages = {
            "required": "This field is required.",
            "null": "This field may not be null.",
            **(error_messages or {}),
        }
        self.read_only, self.write_only, self.default = read_only, write_only, default
        self.required = not read_only and default is empty if required is None else required
        self.allow_null, self.source = allow_null, source

    async def run_validation(self, value: Any = empty) -> Any:
        if value is empty:
            if self.default is not empty:
                return self.default() if callable(self.default) else deepcopy(self.default)
            if self.required:
                raise ValidationError(self.error_messages["required"])
            return empty
        if value is None:
            if not self.allow_null:
                raise ValidationError(self.error_messages["null"])
            return None
        value = await self.to_internal_value(value)
        for validator in self.validators:
            await invoke(validator, value)
        return value

    async def to_internal_value(self, data: Any) -> Any:
        return data

    def to_representation(self, value: Any) -> Any:
        return _representation(value)


class _FormField(Field):
    form_field: Any

    def __init__(self, **kwargs: Any) -> None:
        form_options = {
            key: kwargs.pop(key)
            for key in tuple(kwargs)
            if key
            in {
                "max_length",
                "min_length",
                "max_value",
                "min_value",
                "max_digits",
                "decimal_places",
                "choices",
            }
        }
        super().__init__(**kwargs)
        self._field = self.form_field(required=False, **form_options)

    async def to_internal_value(self, data: Any) -> Any:
        try:
            if data == "" and isinstance(self, (IntegerField, FloatField, DecimalField)):
                raise FormError("Invalid number.", code="invalid")
            return await self._field.clean(data)
        except FormError as exc:
            if exc.code == "invalid":
                detail = (
                    "A valid integer is required."
                    if isinstance(self, IntegerField)
                    else "A valid number is required."
                    if isinstance(self, (FloatField, DecimalField))
                    else exc.messages
                )
            elif exc.code == "max_length":
                detail = f"Ensure this field has no more than {self._field.max_length} characters."
            elif exc.code == "min_length":
                detail = f"Ensure this field has at least {self._field.min_length} characters."
            else:
                detail = exc.messages
            raise ValidationError(detail) from exc


class CharField(_FormField):
    form_field = forms.CharField

    def __init__(
        self, *, allow_blank: bool = False, trim_whitespace: bool = True, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.allow_blank = allow_blank
        self._field.strip = trim_whitespace

    async def to_internal_value(self, data: Any) -> Any:
        if not isinstance(data, (str, int, float)) or isinstance(data, bool):
            raise ValidationError("Not a valid string.")
        value = await super().to_internal_value(data)
        if value == "" and not self.allow_blank:
            raise ValidationError("This field may not be blank.")
        return value


class IntegerField(_FormField):
    form_field = forms.IntegerField


class FloatField(_FormField):
    form_field = forms.FloatField


class DecimalField(_FormField):
    form_field = forms.DecimalField

    def to_representation(self, value: Any) -> Any:
        value = Decimal(str(value))
        places = self._field.decimal_places
        return (
            str(value.quantize(Decimal("1").scaleb(-places))) if places is not None else str(value)
        )


class BooleanField(Field):
    async def to_internal_value(self, data: Any) -> bool:
        if data in (True, 1, "1", "true", "True", "yes", "on"):
            return True
        if data in (False, 0, "0", "false", "False", "no", "off"):
            return False
        raise ValidationError("Must be a valid boolean.")


class DateField(_FormField):
    form_field = forms.DateField


class DateTimeField(_FormField):
    form_field = forms.DateTimeField


class TimeField(_FormField):
    form_field = forms.TimeField


class UUIDField(_FormField):
    form_field = forms.UUIDField


class ChoiceField(_FormField):
    form_field = forms.ChoiceField


class JSONField(Field):
    pass


class ReadOnlyField(Field):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(read_only=True, **kwargs)
