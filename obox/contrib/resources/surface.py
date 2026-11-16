"""Per-resource binding shared by independently customizable surfaces."""

from __future__ import annotations

from typing import Any

from obox.core.exceptions import ImproperlyConfigured


class _Presentation:
    def __init__(self, *, builder: Any = None, service: Any = None) -> None:
        self._builder = builder
        self._service = service

    @property
    def builder(self) -> Any:
        if self._builder is None:
            raise ImproperlyConfigured("Bind the surface to a builder before using it.")
        return self._builder

    @property
    def service(self) -> Any:
        if self._service is None:
            raise ImproperlyConfigured("Bind the surface to a service before using it.")
        return self._service

    def bind(self, builder: Any, service: Any) -> None:
        self._builder, self._service = builder, service
