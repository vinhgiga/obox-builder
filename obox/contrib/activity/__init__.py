"""Public activity feature."""

from obox.contrib.activity import tracking as _tracking  # noqa: F401
from obox.contrib.activity.apps import Activity

__all__ = ["Activity"]
