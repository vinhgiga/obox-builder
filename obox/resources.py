"""Shared SQLAlchemy resource operations for HTML and REST adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import cached_property
from typing import Any, Generic, Literal, TypeVar, cast
from uuid import UUID

import sqlalchemy as sa
from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstanceState, Mapper, selectinload, undefer
from sqlalchemy.sql.elements import UnaryExpression

from obox._async import invoke
from obox._exceptions import BuilderValidationError, NotFound, PermissionDenied, ResourceConflict
from obox.core.exceptions import ImproperlyConfigured
from obox.core.pagination import ListResult, Page
from obox.core.principal import ANONYMOUS, Identity
from obox.db import _scope
from obox.db._fields import _model_value, inspect_fields, normalize, schema
from obox.services import BaseService

ModelT = TypeVar("ModelT")
WriteAction = Literal["create", "update", "delete"]


class Resource(Generic[ModelT]):
    """Declare fields, visibility and write rules once for both adapters."""

    model: type[ModelT]
    schema: type[BaseModel] | None = None
    create_schema: type[BaseModel] | None = None
    update_schema: type[BaseModel] | None = None
    service_class: type[BaseService] | None = None
    load_options: Sequence[Any] = ()
    page_size: int | None = None
    lookup_field: str | None = None
    fields: Sequence[str] | None = None
    readonly_fields: Sequence[str] = ()
    ordering: Sequence[str] = ()

    def __init__(
        self,
        session: AsyncSession | None = None,
        principal: Identity = ANONYMOUS,
        *,
        model: type[ModelT] | None = None,
    ) -> None:
        self.session, self.principal = session, principal
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
        if len(mapper.primary_key) != 1:
            raise ImproperlyConfigured("Resources require one primary key.")
        if self.lookup_field is not None and self.lookup_field not in self.specs:
            raise ImproperlyConfigured("Choose a mapped lookup field.")
        if self.page_size is not None and (type(self.page_size) is not int or self.page_size < 1):
            raise ImproperlyConfigured("Choose a positive page size.")
        if self.service_class is not None and (
            not isinstance(self.service_class, type)
            or not issubclass(self.service_class, BaseService)
        ):
            raise ImproperlyConfigured("service_class must extend BaseService.")
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
        if (self.create_schema or self.schema) is not None:
            declaration = self.create_schema or self.schema
            if not isinstance(declaration, type) or not issubclass(declaration, BaseModel):
                raise ImproperlyConfigured("Resource.schema must be a Pydantic model.")
            normalize(declaration.model_fields, self.model, dict.fromkeys(self.writable))
            names = set(declaration.model_fields)
            if isinstance(self.update_schema, type) and issubclass(self.update_schema, BaseModel):
                normalize(self.update_schema.model_fields, self.model, dict.fromkeys(self.writable))
                names.update(self.update_schema.model_fields)
            self.writable = tuple(name for name in self.writable if name in names)
        self._create_schema = (
            self.create_schema
            or self.schema
            or schema(f"{self.model.__name__}Write", self.specs, self.writable)
        )
        self._patch_schema = (
            self.update_schema
            or self.create_schema
            or self.schema
            or schema(f"{self.model.__name__}Changes", self.specs, self.writable, partial=True)
        )
        if isinstance(self.ordering, str) or not all(
            isinstance(term, str) for term in self.ordering
        ):
            raise ImproperlyConfigured("Ordering must be a sequence of field names.")
        normalize((term.removeprefix("-") for term in self.ordering), self.model, exposed)
        if self.update_schema is not None:
            if not isinstance(self.update_schema, type) or not issubclass(
                self.update_schema, BaseModel
            ):
                raise ImproperlyConfigured("Resource.update_schema must be a Pydantic model.")
            normalize(self.update_schema.model_fields, self.model, dict.fromkeys(self.writable))
        sa.select(self.model).options(*self.load_options)

    @classmethod
    def check(cls) -> None:
        cls()

    def bind(self, session: AsyncSession, principal: Identity) -> Resource[ModelT]:
        if self.session is session and self.principal is principal:
            return self
        return type(self)(session, principal, model=self.model)

    def _session(self) -> AsyncSession:
        if self.session is None:
            raise ImproperlyConfigured("Bind the Resource to an active session.")
        return self.session

    @cached_property
    def service(self) -> BaseService:
        if self.service_class is None:
            raise ImproperlyConfigured("Define service_class before using resource.service.")
        return self.service_class(self._session(), self.principal)

    def get_schema(self, action: str) -> type[BaseModel]:
        return (
            self._create_schema
            if action == "create"
            else self.update_schema or self.create_schema or self.schema or self._create_schema
        )

    def get_queryset(self) -> sa.Select[tuple[ModelT]]:
        return sa.select(self.model).options(*self.load_options)

    def _query(self, query: Any = None) -> Any:
        base = _scope(self.get_queryset(), self.model)
        if query is None:
            return base
        query = _scope(query, self.model)
        return query.where(self.pk.in_(base.with_only_columns(self.pk, maintain_column_froms=True)))

    async def check_permission(self, action: str, obj: ModelT | None = None) -> None:
        if action in {"create", "update", "destroy"}:
            await self._authorize(
                cast(WriteAction, "delete" if action == "destroy" else action), obj, self.principal
            )

    async def get_object(
        self,
        lookup: Any,
        *,
        action: str | None = "retrieve",
        queryset: Any = None,
        for_update: bool = False,
    ) -> ModelT:
        query = self._query(queryset)
        column = getattr(self.model, self.lookup_field) if self.lookup_field else self.pk
        try:
            key = TypeAdapter(column.type.python_type).validate_python(lookup)
        except (TypeError, ValueError) as exc:
            raise NotFound("Record is unavailable.") from exc
        query = query.where(column == key)
        if for_update:
            query = query.with_for_update(of=self.model).execution_options(populate_existing=True)
        obj = (await self._session().scalars(self._load_fields(query))).unique().one_or_none()
        if obj is None:
            raise NotFound("Record is unavailable.")
        if action is not None:
            await self.check_permission(action, obj)
        return cast(ModelT, obj)

    async def _write_object(self, obj: ModelT, action: str) -> ModelT:
        state = cast("InstanceState[ModelT]", sa.inspect(obj))
        if state.identity is None:
            raise NotFound("Record is unavailable.")
        query = (
            self._query()
            .where(self.pk == state.identity[0])
            .with_for_update(of=self.model)
            .execution_options(populate_existing=True)
        )
        found = (await self._session().scalars(self._load_fields(query))).unique().one_or_none()
        if found is None:
            raise NotFound("Record is unavailable.")
        await self.check_permission(action, found)
        return cast(ModelT, found)

    async def reload(self, obj: ModelT) -> ModelT:
        state = cast("InstanceState[ModelT]", sa.inspect(obj))
        if state.identity is None:
            return obj
        query = (
            self._query()
            .where(self.pk == state.identity[0])
            .execution_options(populate_existing=True)
        )
        found = (await self._session().scalars(self._load_fields(query))).unique().one_or_none()
        if found is None:
            raise NotFound("Record is unavailable.")
        return cast(ModelT, found)

    async def ensure_loaded(self, obj: ModelT, fields: Sequence[str]) -> None:
        missing = cast("InstanceState[ModelT]", sa.inspect(obj)).unloaded.intersection(fields)
        if missing:
            await self._session().refresh(obj, attribute_names=list(missing))

    async def list(
        self,
        *,
        queryset: Any = None,
        limit: int | None = None,
        offset: int = 0,
        include_total: bool = False,
    ) -> ListResult[ModelT]:
        if offset < 0 or (limit is not None and limit < 1):
            raise ValueError("Use a positive limit and nonnegative offset.")
        await self.check_permission("list")
        query = self._query(queryset)
        if not query._order_by_clauses:
            query = query.order_by(*self.get_ordering())
        else:
            query = query.order_by(self.pk.asc())
        session = self._session()
        total = await self._count(session, query) if include_total else None
        rows = await self._rows(session, query, offset=offset, limit=limit + 1 if limit else None)
        return ListResult(
            rows[:limit], offset, limit, limit is not None and len(rows) > limit, total
        )

    async def paginate(
        self,
        number: int = 1,
        *,
        page_size: int | None = None,
        queryset: Any = None,
    ) -> Page[ModelT]:
        size = page_size if page_size is not None else self.page_size
        if size is not None and size < 1:
            raise ImproperlyConfigured("Choose a positive page size.")
        if number < 1:
            raise NotFound("Invalid page.")
        result = await self.list(
            queryset=queryset,
            limit=size,
            offset=(number - 1) * size if size else 0,
            include_total=True,
        )
        total = result.total or 0
        page = Page(result.items, number, size or max(1, total), total)
        if number > page.num_pages:
            raise NotFound("Invalid page.")
        return page

    async def create(
        self,
        data: Mapping[str, Any] | BaseModel,
        *,
        validate: bool = True,
        instance: ModelT | None = None,
    ) -> ModelT:
        await self.check_permission("create")
        values = data.model_dump(by_alias=False) if isinstance(data, BaseModel) else data
        values = await self._validate(
            self._session(), self.principal, values, None, validate=validate
        )
        try:
            if instance is not None and type(self).perform_create is Resource.perform_create:
                return await self._persist(
                    self._session(), self.principal, values, new_instance=instance
                )
            return await self.perform_create(values)
        except IntegrityError as exc:
            raise ResourceConflict(
                "The transaction conflicts with current database state."
            ) from exc

    async def update(
        self,
        obj: ModelT,
        data: Mapping[str, Any] | BaseModel,
        *,
        partial: bool = False,
        validate: bool = True,
    ) -> ModelT:
        obj = await self._write_object(obj, "update")
        values = (
            data.model_dump(exclude_unset=partial, by_alias=False)
            if isinstance(data, BaseModel)
            else data
        )
        values = await self._validate(
            self._session(), self.principal, values, obj, validate=validate, partial=partial
        )
        try:
            return await self.perform_update(obj, values, partial=partial)
        except IntegrityError as exc:
            raise ResourceConflict(
                "The transaction conflicts with current database state."
            ) from exc

    async def destroy(self, obj: ModelT) -> None:
        obj = await self._write_object(obj, "destroy")
        try:
            await self.perform_destroy(obj)
        except IntegrityError as exc:
            raise ResourceConflict(
                "The transaction conflicts with current database state."
            ) from exc

    async def perform_create(self, data: dict[str, Any]) -> ModelT:
        return await self._persist(self._session(), self.principal, data)

    async def perform_update(
        self,
        obj: ModelT,
        data: dict[str, Any],
        *,
        partial: bool = False,
    ) -> ModelT:
        return await self._persist(self._session(), self.principal, data, obj)

    async def perform_destroy(self, obj: ModelT) -> None:
        await self._session().delete(obj)
        await self._session().flush()

    async def save_related(self, obj: ModelT, relations: Mapping[str, Any]) -> None:
        session = self._session()
        mapper = cast("Mapper[ModelT]", sa.inspect(self.model))
        for name in relations:
            relation = mapper.relationships.get(name)
            if (
                relation is None
                or relation.viewonly
                or (relation.secondary is None and relation.uselist)
            ):
                raise ImproperlyConfigured("Choose an editable relationship.")
        session.add(obj)
        missing = cast("InstanceState[ModelT]", sa.inspect(obj)).unloaded.intersection(relations)
        if missing:
            await session.refresh(obj, attribute_names=list(missing))

        def assign(sync_session: Any) -> None:
            for name, value in relations.items():
                if mapper.relationships[name].uselist:
                    value = [sync_session.merge(item, load=False) for item in value]
                elif value is not None:
                    value = sync_session.merge(value, load=False)
                setattr(obj, name, value)

        await session.run_sync(assign)
        await session.flush()

    async def validate_constraints(
        self,
        values: Mapping[str, Any],
        obj: ModelT | None = None,
        *,
        unique: bool = False,
    ) -> None:
        mapper = cast("Mapper[ModelT]", sa.inspect(self.model))
        errors: dict[str, list[str]] = {}
        session = self._session()
        with session.no_autoflush:
            for name, prop in mapper.column_attrs.items():
                if name not in values or values[name] is None:
                    continue
                for key in prop.columns[0].foreign_keys:
                    if (
                        await session.scalar(
                            sa.select(key.column).where(key.column == values[name]).limit(1)
                        )
                        is None
                    ):
                        errors[name] = [
                            "Select a valid choice. "
                            "That choice is not one of the available choices."
                        ]
            if unique:
                for constraint in cast(sa.Table, mapper.local_table).constraints:
                    if not isinstance(constraint, (sa.UniqueConstraint, sa.PrimaryKeyConstraint)):
                        continue
                    names = tuple(column.key for column in constraint.columns)
                    if not all(name in values and values[name] is not None for name in names):
                        continue
                    query = sa.select(self.pk).where(
                        *(
                            getattr(self.model, name)
                            == _model_value(mapper.column_attrs[name].columns[0], values[name])
                            for name in names
                        )
                    )
                    if (
                        obj is not None
                        and not cast("InstanceState[ModelT]", sa.inspect(obj)).transient
                    ):
                        query = query.where(self.pk != getattr(obj, self.pk_name))
                    if await session.scalar(query.limit(1)) is not None:
                        label = " and ".join(name.replace("_", " ") for name in names)
                        errors[names[0] if len(names) == 1 else "__all__"] = [
                            f"{self.model.__name__} with this {label} already exists."
                        ]
        if errors:
            raise BuilderValidationError(errors)

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
            *self.load_options,
            *(
                selectinload(getattr(self.model, name))
                if "obox_files" in self.specs[name].column.info
                else undefer(getattr(self.model, name))
                for name in self.readable
            ),
        )

    async def _authorize(self, action: WriteAction, obj: ModelT | None, actor: Any) -> None:
        if not await invoke(self.can, action, obj, actor):
            raise PermissionDenied("This operation is not permitted.")

    async def writable_fields(self, action: WriteAction, obj: ModelT | None = None) -> set[str]:
        return await self._writable(action, obj, self.principal)

    async def _writable(self, action: WriteAction, obj: ModelT | None, actor: Any) -> set[str]:
        definition = self._create_schema if action == "create" else self._patch_schema
        return set(self.writable).intersection(
            definition.model_fields, await invoke(self.get_writable_fields, action, obj, actor)
        )

    def get_ordering(self) -> Sequence[Any]:
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
                and type(self).perform_create is Resource.perform_create
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
        if limit is not None or offset:
            query = _scope(query, self.model)
            order_columns = []
            for term in query._order_by_clauses:
                while isinstance(term, UnaryExpression):
                    term = term.element
                order_columns.append(term)
            identities = (
                query.with_only_columns(self.pk, *order_columns, maintain_column_froms=True)
                .distinct()
                .offset(offset)
                .limit(limit)
                .subquery()
            )
            query = query.where(self.pk.in_(sa.select(identities.c[0])))
        return (await session.scalars(self._load_fields(query))).unique().all()

    async def _validate(
        self,
        session: AsyncSession,
        actor: Any,
        values: Mapping[str, Any],
        obj: ModelT | None,
        *,
        validate: bool = True,
        partial: bool = True,
    ) -> dict[str, Any]:
        action: WriteAction = "create" if obj is None else "update"
        denied = values.keys() - await self._writable(action, obj, actor)
        if denied:
            raise PermissionDenied(f"Fields cannot be written: {', '.join(sorted(denied))}.")
        if not validate:
            return dict(values)
        input_model = (
            self._create_schema
            if obj is None
            else self._patch_schema
            if partial
            else self.get_schema("update")
        )
        data = dict(values)
        if (
            obj is not None
            and partial
            and (self.schema or self.update_schema or self.create_schema)
        ):
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
                name = ".".join(str(part) for part in error["loc"]) or "__all__"
                errors.setdefault(name, []).append(error["msg"])
            raise BuilderValidationError(errors) from exc
        names = (
            input_model.model_fields.keys()
            if obj is None and (self.schema or self.create_schema)
            else values.keys()
        )
        return {name: getattr(parsed, name) for name in names}

    async def _persist(
        self,
        session: AsyncSession,
        actor: Any,
        values: Mapping[str, Any],
        obj: ModelT | None = None,
        *,
        new_instance: ModelT | None = None,
    ) -> ModelT:
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
            obj = new_instance if new_instance is not None else self.model()
            for name, value in normalized.items():
                setattr(obj, name, value)
            session.add(obj)
        else:
            session.add(obj)
            for name, value in normalized.items():
                setattr(obj, name, value)
        await session.flush()
        return obj


def resolve_resource(target: type[ModelT] | type[Resource[ModelT]]) -> Resource[ModelT]:
    if not isinstance(target, type):
        raise ImproperlyConfigured("Register a SQLAlchemy model or Resource class.")
    if issubclass(target, Resource):
        return target()
    return Resource(model=target)
