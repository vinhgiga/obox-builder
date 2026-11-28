"""Build isolated runtime instances for integration tests."""

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import sqlalchemy as sa
from fastapi import FastAPI
from obox.apps import AppConfig
from obox.resources import ModelResource
from obox.runtime.config import RuntimeConfig
from obox.runtime.runtime import _Runtime
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


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
        options["fields"] = tuple(fields)
    return type(model.__name__ + "Resource", (ModelResource,), {"model": model, **options})


def collection_filter(*terms: tuple[str, str, Any]) -> str:
    return json.dumps(
        {
            "conjunction": "and",
            "children": [{"field": field, "op": op, "value": value} for field, op, value in terms],
        }
    )


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
