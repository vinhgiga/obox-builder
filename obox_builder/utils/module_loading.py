from __future__ import annotations

from importlib import import_module
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
