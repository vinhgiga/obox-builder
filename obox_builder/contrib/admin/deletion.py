"""Compatibility imports for deletion services and the HTML preview."""

import time as time

from obox_builder.contrib.admin.services.deletion import (
    CONFIRM_SECONDS,
    MAX_OBJECTS,
    DeleteEffect,
    DeletionPlan,
    collect_deletion,
    confirmation_token,
    confirmed,
)
from obox_builder.contrib.admin.views.deletion import deletion_page

__all__ = [
    "CONFIRM_SECONDS",
    "MAX_OBJECTS",
    "DeleteEffect",
    "DeletionPlan",
    "collect_deletion",
    "confirmation_token",
    "confirmed",
    "deletion_page",
]
