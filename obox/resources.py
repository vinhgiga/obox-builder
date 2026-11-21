"""Shared SQLAlchemy resource operations for HTML and REST adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Generic, Literal, TypeVar, cast
from uuid import UUID

import sqlalchemy as sa
from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstanceState, Mapper, selectinload, undefer

from obox._async import invoke
from obox._exceptions import BuilderValidationError, NotFound, PermissionDenied
from obox.core.exceptions import ImproperlyConfigured
from obox.db import _scope
from obox.db._fields import _model_value, inspect_fields, normalize, schema

ModelT = TypeVar("ModelT")
WriteAction = Literal["create", "update", "delete"]


class Resource(Generic[ModelT]):
    """Declare fields, visibility and write rules once for both adapters."""

    model: type[ModelT]
    schema: type[BaseModel] | None = None
    fields: Sequence[str] | None = None
    readonly_fields: Sequence[str] = ()
    ordering: Sequence[str] = ()

    def __init__(self, model: type[ModelT] | None = None) -> None:
        if model is not None:
            self.model = model
        if not isinstance(getattr(self, "model", None), type):
            raise ImproperlyConfigured("Define a mapped model on the Resource.")
        self.specs = inspect_fields(self.model)
        mapper = cast("Mapper[ModelT]", sa.inspect(self.model))
        self.pk = mapper.primary_key[0]
        self.pk_name = mapper.get_property_by_column(self.pk).key
        if self.pk_name not in self.specs or self.specs[
            self.pk_name
        ].column.type.python_type not in (int, str, UUID):
            raise ImproperlyConfigured("Resources require an int, str or UUID primary key.")
        exposed = {name: field for name, field in self.specs.items() if field.exposed}
        self.readable = (
            normalize(self.fields, self.model, exposed)
            if self.fields is not None
            else tuple(
                name for name, spec in exposed.items() if not isinstance(spec.column.type, sa.JSON)
            )
        )
        readonly = normalize(self.readonly_fields, self.model, exposed)
        self.writable = tuple(
            name for name in self.readable if not self.specs[name].readonly and name not in readonly
        )
        if self.schema is not None:
            if not isinstance(self.schema, type) or not issubclass(self.schema, BaseModel):
                raise ImproperlyConfigured("Resource.schema must be a Pydantic model.")
            normalize(self.schema.model_fields, self.model, dict.fromkeys(self.writable))
            self.writable = tuple(self.schema.model_fields)
        self._create_schema = self.schema or schema(
            f"{self.model.__name__}Write", self.specs, self.writable
        )
        self._patch_schema = self.schema or schema(
            f"{self.model.__name__}Changes", self.specs, self.writable, partial=True
        )
        normalize((term.removeprefix("-") for term in self.ordering), self.model, exposed)

    def get_queryset(self, actor: Any) -> sa.Select[tuple[ModelT]]:
        return sa.select(self.model)

    def _query(self, actor: Any) -> sa.Select[tuple[ModelT]]:
        return cast("sa.Select[tuple[ModelT]]", _scope(self.get_queryset(actor), self.model))

    async def can(self, action: WriteAction, obj: ModelT | None, actor: Any) -> bool:
        return True

    def get_writable_fields(
        self, action: WriteAction, obj: ModelT | None, actor: Any
    ) -> Sequence[str]:
        return self.writable

    def prepare_write(
        self, values: dict[str, Any], obj: ModelT | None, actor: Any
    ) -> dict[str, Any]:
        """Add trusted values or normalize validated input before persistence."""
        return values

    def _load_fields(self, query: Any) -> Any:
        return query.options(
            *(
                selectinload(getattr(self.model, name))
                if "obox_files" in self.specs[name].column.info
                else undefer(getattr(self.model, name))
                for name in self.readable
            )
        )

    async def _authorize(self, action: WriteAction, obj: ModelT | None, actor: Any) -> None:
        if not await invoke(self.can, action, obj, actor):
            raise PermissionDenied("This operation is not permitted.")

    async def _writable(self, action: WriteAction, obj: ModelT | None, actor: Any) -> set[str]:
        return set(self.writable).intersection(
            await invoke(self.get_writable_fields, action, obj, actor)
        )

    def _order(self) -> list[Any]:
        columns = [getattr(self.model, name.removeprefix("-")) for name in self.ordering]
        return [
            column.desc() if name.startswith("-") else column.asc()
            for name, column in zip(self.ordering, columns, strict=True)
        ] + [self.pk.asc()]

    def _check_create(self, writable: Sequence[str]) -> None:
        mapper = cast("Mapper[ModelT]", sa.inspect(self.model))
        for prop in mapper.column_attrs:
            column = prop.columns[0]
            if (
                isinstance(column, sa.Column)
                and not column.nullable
                and column.default is None
                and column.server_default is None
                and column is not column.table.autoincrement_column
                and prop.key not in writable
                and type(self).prepare_write is Resource.prepare_write
            ):
                raise ImproperlyConfigured(
                    f"{self.model.__name__}.{prop.key} requires a value for resource creation."
                )

    async def _count(self, session: AsyncSession, query: Any) -> int:
        identities = (
            _scope(query, self.model)
            .with_only_columns(self.pk, maintain_column_froms=True)
            .order_by(None)
            .distinct()
        )
        return int(
            await session.scalar(sa.select(sa.func.count()).select_from(identities.subquery())) or 0
        )

    async def _rows(
        self, session: AsyncSession, query: Any, *, offset: int = 0, limit: int | None = None
    ) -> Sequence[ModelT]:
        if limit is not None:
            query = query.offset(offset).limit(limit)
        return (await session.scalars(self._load_fields(query))).unique().all()

    async def _get(
        self, session: AsyncSession, query: Any, key: Any, *, lock: bool = False
    ) -> ModelT:
        try:
            key = TypeAdapter(self.pk.type.python_type).validate_python(key)
        except (TypeError, ValueError) as exc:
            raise NotFound("Record is unavailable.") from exc
        query = _scope(query, self.model).where(self.pk == key)
        if lock:
            query = query.with_for_update(of=self.model).execution_options(populate_existing=True)
        obj = (await session.scalars(self._load_fields(query))).unique().one_or_none()
        if obj is None:
            raise NotFound("Record is unavailable.")
        return cast(ModelT, obj)

    async def _validate(
        self,
        session: AsyncSession,
        actor: Any,
        values: Mapping[str, Any],
        obj: ModelT | None,
        *,
        validate: bool = True,
    ) -> dict[str, Any]:
        action: WriteAction = "create" if obj is None else "update"
        await self._authorize(action, obj, actor)
        denied = values.keys() - await self._writable(action, obj, actor)
        if denied:
            raise PermissionDenied(f"Fields cannot be written: {', '.join(sorted(denied))}.")
        if not validate and self.schema is None:
            return dict(values)
        input_model = self._create_schema if obj is None else self._patch_schema
        data = dict(values)
        if obj is not None and self.schema is not None:
            missing = input_model.model_fields.keys() - values.keys()
            unloaded = cast("InstanceState[ModelT]", sa.inspect(obj)).unloaded.intersection(missing)
            if unloaded:
                await session.refresh(obj, attribute_names=list(unloaded))
            data.update({name: self.specs[name].serialize(getattr(obj, name)) for name in missing})
        try:
            parsed = input_model.model_validate(data, by_name=True, by_alias=False)
        except ValidationError as exc:
            errors: dict[str, list[str]] = {}
            for error in exc.errors(include_input=False, include_url=False):
                name = str(error["loc"][0]) if error["loc"] else "__all__"
                errors.setdefault(name, []).append(error["msg"])
            raise BuilderValidationError(errors) from exc
        return {name: getattr(parsed, name) for name in values}

    async def _save(
        self,
        session: AsyncSession,
        actor: Any,
        values: Mapping[str, Any],
        obj: ModelT | None = None,
        *,
        validate: bool = True,
    ) -> ModelT:
        values = await self._validate(session, actor, values, obj, validate=validate)
        values = await invoke(self.prepare_write, values, obj, actor)
        mapper = cast("Mapper[ModelT]", sa.inspect(self.model))
        with session.no_autoflush:
            normalized = dict(values)
            for name in values.keys() & mapper.column_attrs.keys():
                column = mapper.column_attrs[name].columns[0]
                value = values[name]
                if value is None and not column.nullable and not column.type.should_evaluate_none:
                    raise BuilderValidationError({name: ["This field cannot be null."]})
                try:
                    normalized[name] = _model_value(column, value)
                except (TypeError, ValueError) as exc:
                    raise BuilderValidationError({name: ["Invalid model value."]}) from exc
            for table in mapper.tables:
                if not isinstance(table, sa.Table):
                    continue
                for fk in table.foreign_key_constraints:
                    names = [mapper.get_property_by_column(key.parent).key for key in fk.elements]
                    written = values.keys() & set(names)
                    if not written:
                        continue
                    if obj is not None and (
                        missing := cast(
                            "InstanceState[ModelT]", sa.inspect(obj)
                        ).unloaded.intersection(names)
                        - written
                    ):
                        await session.refresh(obj, attribute_names=list(missing))
                    keys = [
                        normalized[name] if name in normalized else getattr(obj, name, None)
                        for name in names
                    ]
                    if (
                        None not in keys
                        and await session.scalar(
                            sa.select(sa.literal(True))
                            .where(
                                *(
                                    key.column == value
                                    for key, value in zip(fk.elements, keys, strict=True)
                                )
                            )
                            .limit(1)
                        )
                        is None
                    ):
                        raise BuilderValidationError(
                            {min(written): ["Referenced record is unavailable."]}
                        )
            files = [
                name
                for name in values.keys() & mapper.relationships.keys()
                if "obox_files" in mapper.relationships[name].info
            ]
            if files:
                ctx = session.info.get("obox._context")
                feature = ctx._owner._feature("files") if ctx is not None else None
                if feature is None:
                    raise ImproperlyConfigured("Install Files before writing attachments.")
                normalized = await feature.resolve(self.model, normalized, ctx, obj)
                if obj is not None and (
                    missing := cast("InstanceState[ModelT]", sa.inspect(obj)).unloaded.intersection(
                        files
                    )
                ):
                    await session.refresh(obj, attribute_names=list(missing))
        if obj is None:
            for name, value in normalized.items():
                spec = self.specs.get(name)
                if value is None and spec is not None and not spec.column.type.should_evaluate_none:
                    normalized[name] = sa.null()
            obj = self.model(**normalized)
            session.add(obj)
        else:
            for name, value in normalized.items():
                setattr(obj, name, value)
        await session.flush()
        return obj

    async def _delete(self, session: AsyncSession, actor: Any, obj: ModelT) -> None:
        await self._authorize("delete", obj, actor)
        await session.delete(obj)
        await session.flush()


def resolve_resource(target: type[ModelT] | type[Resource[ModelT]]) -> Resource[ModelT]:
    if not isinstance(target, type):
        raise ImproperlyConfigured("Register a SQLAlchemy model or Resource class.")
    if issubclass(target, Resource):
        return target()
    return Resource(target)
