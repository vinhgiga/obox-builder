"""Domain errors raised by the comment service and mapped by the views."""

from __future__ import annotations


class CommentError(Exception):
    """Base class for expected comment failures."""


class CommentValidationError(CommentError):
    """Invalid input (body, parent, mentions). ``field`` names the form input."""

    def __init__(self, message: str, *, field: str = "body") -> None:
        super().__init__(message)
        self.field = field
        self.message = message


class CommentPermissionDenied(CommentError):
    """The viewer may not perform the action."""


class CommentNotFound(CommentError):
    """The comment does not exist on this target row (or the row is gone)."""


class CommentRateLimited(CommentError):
    """Too many comments in the configured window."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Too many comments; retry in {retry_after} second(s).")
        self.retry_after = retry_after


class CommentConflict(CommentError):
    """A stale write or a reused idempotency key on a different row."""


__all__ = [
    "CommentConflict",
    "CommentError",
    "CommentNotFound",
    "CommentPermissionDenied",
    "CommentRateLimited",
    "CommentValidationError",
]
