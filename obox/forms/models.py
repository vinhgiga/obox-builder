"""Model forms derive their fields from native SQLAlchemy model metadata."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa

from obox.core.exceptions import ImproperlyConfigured
from obox.db import _session
from obox.db._fields import _model_value
from obox.forms.fields import (
    BooleanField,
    CharField,
    ChoiceField,
    DateField,
    DateTimeField,
    DecimalField,
    Field,
    FloatField,
    IntegerField,
    JSONField,
    TimeField,
    UUIDField,
)
from obox.forms.forms import Form


def fields_for_model(
    model: type[Any],
    fields: Any = None,
    exclude: Any = None,
    *,
    widgets: Any = None,
    labels: Any = None,
    help_texts: Any = None,
) -> dict[str, Field]:
    mapper = sa.inspect(model)
    names = list(mapper.column_attrs.keys()) if fields in (None, "__all__") else list(fields)
    result: dict[str, Field] = {}
    for name in names:
        if name in (exclude or ()):
            continue
        prop = mapper.column_attrs.get(name)
        if prop is None:
            raise ImproperlyConfigured(f"Unknown field {name!r} for {model.__name__}.")
        column = prop.columns[0]
        generated_pk = column.primary_key and (
            (isinstance(column.type, sa.Integer) and column.autoincrement is not False)
            or column.identity is not None
            or column.server_default is not None
        )
        if generated_pk or column.computed is not None or column is mapper.version_id_col:
            if fields not in (None, "__all__"):
                raise ImproperlyConfigured(
                    f"{name!r} cannot be specified as it is a non-editable field."
                )
            continue
        options: dict[str, Any] = {
            "required": not column.nullable
            and column.default is None
            and column.server_default is None,
            "label": (labels or {}).get(name),
            "help_text": (help_texts or {}).get(name, ""),
            "widget": (widgets or {}).get(name),
        }
        if column.default is not None and column.default.is_scalar:
            options["initial"] = column.default.arg
        field: Field
        kind = column.type
        if isinstance(kind, sa.Enum):
            field = ChoiceField(
                choices=[(item.value, item.value) for item in kind.enum_class]
                if kind.enum_class
                else [(item, item) for item in kind.enums],
                **options,
            )
        elif isinstance(kind, sa.String):
            field = CharField(
                max_length=kind.length, empty_value=None if column.nullable else "", **options
            )
        elif isinstance(kind, sa.Boolean):
            field = BooleanField(**{**options, "required": False})
        elif isinstance(kind, sa.Integer):
            field = IntegerField(**options)
        elif isinstance(kind, sa.Float):
            field = FloatField(**options)
        elif isinstance(kind, sa.Numeric):
            field = DecimalField(max_digits=kind.precision, decimal_places=kind.scale, **options)
        elif isinstance(kind, sa.DateTime):
            field = DateTimeField(**options)
        elif isinstance(kind, sa.Date):
            field = DateField(**options)
        elif isinstance(kind, sa.Time):
            field = TimeField(**options)
        elif isinstance(kind, sa.Uuid):
            field = UUIDField(**options)
        elif isinstance(kind, sa.JSON):
            field = JSONField(**options)
        else:
            raise ImproperlyConfigured(f"Unsupported model field: {name!r}.")
        result[name] = field
    return result


class ModelForm(Form):
    @classmethod
    def _collect_fields(cls) -> dict[str, Field]:
        fields: dict[str, Field] = {}
        for parent in reversed(cls.__mro__[1:]):
            fields.update(getattr(parent, "declared_fields", {}))
        for name, value in list(vars(cls).items()):
            if isinstance(value, Field):
                fields[name] = value
                delattr(cls, name)
            elif value is None:
                fields.pop(name, None)
        return fields

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        meta = getattr(cls, "Meta", None)
        model = getattr(meta, "model", None)
        if model is None:
            return
        selected, exclude = getattr(meta, "fields", None), getattr(meta, "exclude", None)
        if selected is None and exclude is None:
            raise ImproperlyConfigured("A ModelForm requires Meta.fields or Meta.exclude.")
        if isinstance(selected, str) and selected != "__all__":
            raise TypeError("Meta.fields cannot be a string. Use a list or tuple.")
        declared = cls.declared_fields
        names = selected if selected != "__all__" and selected is not None else None
        scalar = [name for name in names if name not in declared] if names is not None else names
        generated = fields_for_model(
            model,
            scalar,
            exclude,
            widgets=getattr(meta, "widgets", None),
            labels=getattr(meta, "labels", None),
            help_texts=getattr(meta, "help_texts", None),
        )
        if names is not None:
            generated = {
                name: declared[name] if name in declared else generated[name]
                for name in names
                if name in declared or name in generated
            }
        generated.update(declared)
        cls.base_fields = generated

    def __init__(
        self,
        data: Any = None,
        files: Any = None,
        *,
        instance: Any = None,
        initial: Mapping[str, Any] | None = None,
        prefix: str | None = None,
    ) -> None:
        meta = getattr(self, "Meta", None)
        model = getattr(meta, "model", None)
        if model is None:
            raise ValueError("ModelForm has no model class specified.")
        self.instance = model() if instance is None else instance
        values = {name: getattr(self.instance, name, None) for name in self.base_fields}
        if instance is None:
            values = {name: field.initial for name, field in self.base_fields.items()}
        super().__init__(data, files, initial={**values, **(initial or {})}, prefix=prefix)

    async def _post_clean(self) -> None:
        mapper = sa.inspect(type(self.instance))
        constraints = [
            tuple(column.key for column in constraint.columns)
            for constraint in mapper.local_table.constraints
            if isinstance(constraint, (sa.UniqueConstraint, sa.PrimaryKeyConstraint))
        ]
        foreign = [
            (name, key)
            for name, prop in mapper.column_attrs.items()
            for key in prop.columns[0].foreign_keys
            if name in self.cleaned_data and self.cleaned_data[name] is not None
        ]
        relevant = [
            names
            for names in constraints
            if all(
                name in self.cleaned_data and self.cleaned_data[name] is not None for name in names
            )
        ]
        if not relevant and not foreign:
            return
        async with _session() as session:
            with session.no_autoflush:
                for name, key in foreign:
                    found = await session.scalar(
                        sa.select(key.column).where(key.column == self.cleaned_data[name]).limit(1)
                    )
                    if found is None:
                        self.add_error(
                            name,
                            "Select a valid choice. "
                            "That choice is not one of the available choices.",
                        )
                for names in relevant:
                    query = sa.select(mapper.primary_key[0]).where(
                        *(
                            getattr(type(self.instance), name)
                            == _model_value(
                                mapper.column_attrs[name].columns[0], self.cleaned_data[name]
                            )
                            for name in names
                        )
                    )
                    pk = mapper.get_property_by_column(mapper.primary_key[0]).key
                    if not sa.inspect(self.instance).transient:
                        query = query.where(mapper.primary_key[0] != getattr(self.instance, pk))
                    if await session.scalar(query.limit(1)) is not None:
                        labels = " and ".join(name.replace("_", " ") for name in names)
                        self.add_error(
                            names[0] if len(names) == 1 else None,
                            f"{type(self.instance).__name__} with this {labels} already exists.",
                        )

    async def save(self, commit: bool = True) -> Any:
        if not self._validated:
            await self.full_clean()
        if not self.is_bound or self.errors:
            raise ValueError(
                f"The {type(self.instance).__name__} could not be saved "
                "because the data didn't validate."
            )
        mapper = sa.inspect(type(self.instance))
        for name, value in self.cleaned_data.items():
            if name not in mapper.attrs:
                continue
            column = mapper.column_attrs[name].columns[0] if name in mapper.column_attrs else None
            if (
                column is not None
                and self.add_prefix(name) not in self.data
                and column.default is not None
            ):
                continue
            if column is not None:
                value = _model_value(column, value)
            setattr(self.instance, name, value)
        if commit:
            async with _session() as session:
                session.add(self.instance)
                await session.flush()
        return self.instance


def modelform_factory(
    model: type[Any],
    form: type[ModelForm] = ModelForm,
    *,
    fields: Any = None,
    exclude: Any = None,
    widgets: Any = None,
    labels: Any = None,
    help_texts: Any = None,
) -> type[ModelForm]:
    base_meta = getattr(form, "Meta", object)
    options = {"model": model}
    options.update(
        {
            name: value
            for name, value in (
                ("fields", fields),
                ("exclude", exclude),
                ("widgets", widgets),
                ("labels", labels),
                ("help_texts", help_texts),
            )
            if value is not None
        }
    )
    meta = type("Meta", (base_meta,), options)
    return type(model.__name__ + "Form", (form,), {"Meta": meta})
