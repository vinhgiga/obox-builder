from __future__ import annotations


class OboxError(Exception):
    """Base class for framework errors."""


class ImproperlyConfigured(OboxError):
    """Raised when settings, apps registry or environment are misconfigured."""


class NotAuthenticated(OboxError):
    """Raised when a request has no valid authentication."""


class PermissionDenied(OboxError):
    """Raised when the authenticated user lacks the required permission."""
