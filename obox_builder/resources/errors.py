"""Domain errors translated by HTML and REST adapters."""

from __future__ import annotations

from collections.abc import Mapping

from obox_builder.exceptions import OboxError


class ResourceValidationError(OboxError):
    def __init__(self, errors: Mapping[str, list[str]]) -> None:
        self.errors = dict(errors)
        super().__init__("Invalid operation input.")


class ResourceConflict(OboxError):
    """The requested operation conflicts with current persisted state."""
