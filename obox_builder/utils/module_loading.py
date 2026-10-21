from __future__ import annotations

from collections.abc import Iterable
from importlib import import_module
from importlib.util import find_spec
from typing import Any

from obox_builder.core.exceptions import ImproperlyConfigured


def import_string(dotted_path: str) -> Any:
    """Import a dotted module path and return the attribute/class it designates."""
    module_path, _, class_name = dotted_path.rpartition(".")
    if not module_path or not class_name:
        raise ImproperlyConfigured(f"{dotted_path!r} doesn't look like a module path")

    try:
        module = import_module(module_path)
    except ImportError as exc:
        raise ImproperlyConfigured(f"Module {module_path!r} does not exist: {exc}") from exc

    try:
        return getattr(module, class_name)
    except AttributeError as exc:
        raise ImproperlyConfigured(
            f"Module {module_path!r} does not define {class_name!r}"
        ) from exc


def autodiscover_modules(*names: str, packages: Iterable[str]) -> None:
    """Import optional modules, propagating errors from modules that do exist.

    Packages are supplied by the caller to keep this utility below the app
    registry in the dependency layers.
    """
    for package in packages:
        if not hasattr(import_module(package), "__path__"):
            continue
        for name in names:
            path = f"{package}.{name}"
            if find_spec(path) is not None:
                import_module(path)
