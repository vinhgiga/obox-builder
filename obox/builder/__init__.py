"""Compatibility imports for projects that still list ``obox.builder``.

CRUD declarations now live on ``AppConfig`` and use ``obox.views.generic``.
Keeping this package importable lets older ``INSTALLED_APPS`` settings load
while projects migrate to the current API.
"""

from obox.views.generic import StackedInline, TabularInline, action

__all__ = ["StackedInline", "TabularInline", "action"]
