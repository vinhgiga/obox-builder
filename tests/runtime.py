"""Build isolated runtime instances for integration tests."""

from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI
from obox.apps import AppConfig
from obox.runtime.config import RuntimeConfig
from obox.runtime.runtime import _Runtime


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
        runtime._activate(feature)
    runtime._loading_apps = False
    runtime._finish_apps()
    runtime._bind_templates()
    return runtime
