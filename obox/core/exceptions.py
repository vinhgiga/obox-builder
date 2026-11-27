"""Configuration, validation, and domain exceptions shared across Obox."""

from collections.abc import Mapping
from typing import Any

NON_FIELD_ERRORS = "__all__"


class DomainError(Exception):
    code = "domain_error"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        self.message = message
        if code is not None:
            self.code = code
        super().__init__(message)


class NotAuthenticated(DomainError):
    code = "unauthenticated"


class PermissionDenied(DomainError):
    code = "permission_denied"


class NotFound(DomainError):
    code = "not_found"


class ResourceConflict(DomainError):
    code = "resource_conflict"


class PreconditionFailed(DomainError):
    code = "precondition_failed"


class PreconditionRequired(DomainError):
    code = "precondition_required"


class BuilderValidationError(DomainError):
    def __init__(
        self,
        errors: Mapping[str, list[str]],
        *,
        code: str = "validation_error",
        field_code: str | None = None,
    ) -> None:
        self.field_code = field_code
        self.errors = dict(errors)
        super().__init__("Invalid input.", code=code)


class RuleViolation(DomainError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message, code=code)


class InvalidQuery(DomainError):
    code = "invalid_query"


class ImproperlyConfigured(Exception):
    pass


class AppRegistryNotReady(Exception):
    pass


class ValidationError(Exception):
    message_dict: dict[str, list[str]]
    messages: list[str]

    def __init__(self, message: Any, code: str | None = None, params: Any = None) -> None:
        self.code, self.params = code, params
        if isinstance(message, ValidationError):
            message = message.message_dict if hasattr(message, "message_dict") else message.messages
        if isinstance(message, Mapping):
            self.message_dict = {
                key: ValidationError(value).messages for key, value in message.items()
            }
            self.messages = [value for values in self.message_dict.values() for value in values]
        elif isinstance(message, (list, tuple)):
            self.messages = [text for item in message for text in ValidationError(item).messages]
        else:
            self.messages = [str(message) % params if params else str(message)]
        super().__init__(self.message_dict if hasattr(self, "message_dict") else self.messages)
