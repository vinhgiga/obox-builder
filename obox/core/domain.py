"""Transport-independent failures shared by resources and services."""

from collections.abc import Mapping


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
