"""Build isolated runtime instances for integration tests."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import sqlalchemy as sa
from fastapi import FastAPI
from obox.apps import AppConfig
from obox.db.fields import inspect_fields
from obox.forms.models import _relationship_field
from obox.resources import ModelResource
from obox.runtime.config import RuntimeConfig
from obox.runtime.runtime import _Runtime
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import MANYTOONE


def build_runtime(
    app: FastAPI,
    settings: RuntimeConfig,
    *,
    app_configs: Sequence[AppConfig] = (),
    features: Sequence[Any] = (),
    enforce_permissions: bool = False,
) -> _Runtime:
    runtime = _Runtime(app, settings=settings, defer=True, enforce_permissions=enforce_permissions)
    for config in app_configs:
        runtime.add_app(config)
    for feature in features:
        runtime.activate(feature)
    runtime._loading_apps = False
    runtime.finish_apps()
    runtime.bind_templates()
    return runtime


def resource_for(
    model: type[Any], *, fields: Any = None, **options: Any
) -> type[ModelResource[Any]]:
    if fields is not None and fields != "__all__":
        mapper = sa.inspect(model)
        available = inspect_fields(model)
        selected = []
        for name in fields:
            relation = mapper.relationships.get(name)
            if relation is None or name in available:
                selected.append(name)
            elif relation.direction is MANYTOONE:
                selected.extend(
                    mapper.get_property_by_column(local).key
                    for _, local in relation.synchronize_pairs
                )
        options["fields"] = tuple(dict.fromkeys(selected))
    return type(model.__name__ + "Resource", (ModelResource,), {"model": model, **options})


def relation_controls(model: type[Any], names: Any) -> dict[str, Any]:
    return {
        name: _relationship_field(relation)
        for name in names
        if (relation := sa.inspect(model).relationships.get(name)) is not None
    }


def sessions_for(url: str) -> async_sessionmaker[Any]:
    return async_sessionmaker(create_async_engine(url), expire_on_commit=False)


@contextmanager
def track_checkouts(session_factory: async_sessionmaker[Any]) -> Iterator[list[None]]:
    engine = session_factory.kw["bind"].sync_engine
    checkouts: list[None] = []

    def record_checkout(*_: Any) -> None:
        checkouts.append(None)

    sa.event.listen(engine, "checkout", record_checkout)
    try:
        yield checkouts
    finally:
        sa.event.remove(engine, "checkout", record_checkout)
