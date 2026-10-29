"""Compatibility imports for shared history helpers."""

from obox_builder.contrib.admin.services.history import (
    CARD_LIMIT,
    TIMELINE_PAGE_SIZE,
    DiffMask,
    RevertLinks,
    before_cursor,
    build_card,
    build_page,
    diff_mask,
    entry_view,
    model_timeline_available,
    timeline_available,
    timeline_context,
)

__all__ = [
    "CARD_LIMIT",
    "TIMELINE_PAGE_SIZE",
    "DiffMask",
    "RevertLinks",
    "diff_mask",
    "model_timeline_available",
    "timeline_available",
    "before_cursor",
    "entry_view",
    "build_card",
    "build_page",
    "timeline_context",
]
