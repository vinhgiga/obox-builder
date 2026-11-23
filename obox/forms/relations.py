"""Model choices backed by a SQLAlchemy select used for display and validation."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import sqlalchemy as sa
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.orm import Mapper

from obox.core.async_utils import invoke
from obox.core.exceptions import ImproperlyConfigured, ValidationError
from obox.db.session import _session
from obox.forms.fields import ChoiceField, _type_adapter
from obox.forms.widgets import SelectMultiple


class ModelChoiceField(ChoiceField):
    def __init__(
        self, queryset: Any = None, *, empty_label: str | None = "---------", **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.error_messages.setdefault(
            "invalid_choice",
            "Select a valid choice. That choice is not one of the available choices.",
        )
        self.error_messages.setdefault("invalid_list", "Enter a list of values.")
        self.empty_label = empty_label
        self.queryset = queryset

    def __deepcopy__(self, memo: dict[int, Any]) -> ModelChoiceField:
        result = type(self).__new__(type(self))
        memo[id(self)] = result
        result.__dict__ = {
            name: value if name == "_queryset" else deepcopy(value, memo)
            for name, value in vars(self).items()
        }
        return result

    @property
    def queryset(self) -> Any:
        return self._queryset

    @queryset.setter
    def queryset(self, value: Any) -> None:
        self._queryset = value
        self.choices = []

    def _model(self) -> Any:
        if not isinstance(self.queryset, sa.Select):
            raise ImproperlyConfigured("Set ModelChoiceField.queryset to a model select.")
        descriptions = self.queryset.column_descriptions
        model = descriptions[0].get("entity") if len(descriptions) == 1 else None
        if model is None or descriptions[0]["expr"] is not model:
            raise ImproperlyConfigured("Model choices require select(Model).")
        mapper = sa.inspect(model)
        if not isinstance(mapper, Mapper) or len(mapper.primary_key) != 1:
            raise ImproperlyConfigured("Model choices require one primary key.")
        return mapper

    def prepare_value(self, value: Any) -> Any:
        state = sa.inspect(value, raiseerr=False)
        if state is not None and hasattr(state, "mapper"):
            return getattr(
                value, state.mapper.get_property_by_column(state.mapper.primary_key[0]).key
            )
        return value

    def label_from_instance(self, obj: Any) -> str:
        return str(obj)

    async def prepare(self) -> None:
        mapper = self._model()
        async with _session() as session:
            with session.no_autoflush:
                objects = (await session.scalars(self.queryset)).unique().all()
                self.choices = [
                    (
                        getattr(obj, mapper.get_property_by_column(mapper.primary_key[0]).key),
                        self.label_from_instance(obj),
                    )
                    for obj in objects
                ]
        if self.empty_label is not None:
            self.choices.insert(0, ("", self.empty_label))

    async def _check_values(self, values: list[Any]) -> list[Any]:
        mapper = self._model()
        column = mapper.primary_key[0]
        key = mapper.get_property_by_column(column).key
        try:
            keys = list(
                dict.fromkeys(
                    _type_adapter(column.type.python_type).validate_python(
                        self.prepare_value(value)
                    )
                    for value in values
                )
            )
        except (TypeError, ValueError, PydanticValidationError) as exc:
            raise ValidationError(
                self.error_messages["invalid_choice"], code="invalid_choice"
            ) from exc
        # Keep ORM policy options on the original select, and preserve sliced scopes.
        query = self.queryset
        if not query._has_row_limiting_clause:
            query = query.where(getattr(mapper.class_, key).in_(keys))
        async with _session() as session:
            with session.no_autoflush:
                objects = (await session.scalars(query)).unique().all()
        found = {getattr(obj, key): obj for obj in objects}
        if any(value not in found for value in keys):
            raise ValidationError(
                self.error_messages["invalid_choice"],
                code="invalid_choice",
            )
        return [found[value] for value in keys]

    async def clean(self, value: Any) -> Any:
        if value in self.empty_values:
            if self.required:
                raise ValidationError(self.error_messages["required"], code="required")
            return None
        result = (await self._check_values([value]))[0]
        for validator in self.validators:
            await invoke(validator, result)
        return result


class ModelMultipleChoiceField(ModelChoiceField):
    widget: Any = SelectMultiple

    def __init__(self, queryset: Any = None, **kwargs: Any) -> None:
        super().__init__(queryset, empty_label=None, **kwargs)

    def prepare_value(self, value: Any) -> Any:
        if isinstance(value, (list, tuple, set)):
            return [super(ModelMultipleChoiceField, self).prepare_value(item) for item in value]
        return super().prepare_value(value)

    async def clean(self, value: Any) -> list[Any]:
        if value in self.empty_values:
            if self.required:
                raise ValidationError(self.error_messages["required"], code="required")
            return []
        if not isinstance(value, (list, tuple)):
            raise ValidationError(self.error_messages["invalid_list"], code="invalid_list")
        result = await self._check_values(list(value))
        for validator in self.validators:
            await invoke(validator, result)
        return result
