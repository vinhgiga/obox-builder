"""Signals emitted while recording row history.

* ``pre_record`` — after diffs are computed in ``before_flush``; receivers get
  the list of ``PendingChange`` objects and may mutate or prune it.
* ``post_record`` — right after the history INSERT joins the same transaction
  (deviates from "after commit" for observability; documented in
  ``docs/history.md``).
"""

from obox_builder.core.signals import Signal

pre_record = Signal("history.pre_record")
post_record = Signal("history.post_record")
