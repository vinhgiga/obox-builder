"""Signals fired by the comment service inside the writer's transaction.

Receivers must be fast and must not commit: the comment row, its mentions and
any future notification rows share one transaction. Use the Phase 3 outbox for
durable external effects.
"""

from __future__ import annotations

from obox_builder.core.signals import Signal

#: Fired before a comment row is persisted. ``action`` is one of ``add``,
#: ``edit``, ``delete``, ``resolve``, ``reopen``.
pre_comment = Signal("pre_comment")

#: Fired after the write flushed successfully, in the same transaction.
post_comment = Signal("post_comment")

__all__ = ["post_comment", "pre_comment"]
