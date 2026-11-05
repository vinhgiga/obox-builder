"""Errors shared by services and HTTP adapters."""

from __future__ import annotations

from collections.abc import Mapping


def N_(message: str) -> str:  # noqa: N802
    """Mark a runtime message for gettext extraction."""
    return message


class OboxError(Exception):
    status = 400


class ImproperlyConfigured(OboxError):
    status = 500


class NotAuthenticated(OboxError):
    status = 401


class PermissionDenied(OboxError):
    status = 403


class NotFound(OboxError):
    status = 404


class ResourceConflict(OboxError):
    status = 409


class BuilderValidationError(OboxError):
    status = 422

    def __init__(self, errors: Mapping[str, list[str]], *, code: str = "validation_error") -> None:
        self.code = code
        self.errors = dict(errors)
        super().__init__("Invalid input.")
