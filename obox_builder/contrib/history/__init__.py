"""Row history: an append-only audit trail for every tracked model.

Enable by adding ``obox_builder.contrib.history`` to ``INSTALLED_APPS`` and
declaring ``__history__ = True`` on the models to audit (or calling
``history.track(Model)``). Note: this module intentionally does **not** import
``models``/``capture`` at import time — model registration needs the app
registry to be populated first.
"""

from obox_builder.contrib.history.bulk import bulk_update
from obox_builder.contrib.history.context import HistoryContext, bind, use
from obox_builder.contrib.history.options import HistoryOptions
from obox_builder.contrib.history.tracker import get_tracker, is_tracked, track

#: Permission required to read the row-history timeline in the admin.
HISTORY_VIEW_PERMISSION = "history.rowChange.read"

#: Permission required to revert a recorded change (needs the model op too).
HISTORY_RESTORE_PERMISSION = "history.rowChange.restore"

#: Permission required to export the audit feed (NDJSON/CSV).
HISTORY_EXPORT_PERMISSION = "history.rowChange.export"

__all__ = [
    "HISTORY_EXPORT_PERMISSION",
    "HISTORY_RESTORE_PERMISSION",
    "HISTORY_VIEW_PERMISSION",
    "HistoryContext",
    "HistoryOptions",
    "bind",
    "bulk_update",
    "get_tracker",
    "is_tracked",
    "track",
    "use",
]
