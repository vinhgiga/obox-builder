"""Shared domain errors and runtime gettext markers."""

from obox.core.domain import (
    BatchOperationError as BatchOperationError,
)
from obox.core.domain import (
    BuilderValidationError as BuilderValidationError,
)
from obox.core.domain import (
    DomainError,
)
from obox.core.domain import (
    NotAuthenticated as NotAuthenticated,
)
from obox.core.domain import (
    NotFound as NotFound,
)
from obox.core.domain import (
    PermissionDenied as PermissionDenied,
)
from obox.core.domain import (
    PreconditionFailed as PreconditionFailed,
)
from obox.core.domain import (
    PreconditionRequired as PreconditionRequired,
)
from obox.core.domain import (
    ResourceConflict as ResourceConflict,
)
from obox.core.domain import (
    RuleViolation as RuleViolation,
)

OboxError = DomainError


def N_(message: str) -> str:  # noqa: N802
    """Mark a runtime message for gettext extraction."""
    return message
