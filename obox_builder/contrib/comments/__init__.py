"""Row comments: threaded discussion attached to any admin row.

Enable by adding ``obox_builder.contrib.comments`` to ``INSTALLED_APPS`` and
declaring ``__comments__`` on the models that accept comments. The module stays
light on purpose: models require the app registry, so they are never imported
here. Use ``obox_builder.contrib.comments.service`` / ``queries`` from runtime
code and ``registry.is_commentable`` from checks and admin hooks.
"""

from obox_builder.contrib.comments.options import CommentOptions

#: Permission required to read a row's comment threads.
COMMENTS_VIEW_PERMISSION = "comments.rowComment.read"

#: Permission required to post, reply, edit and delete one's own comments.
COMMENTS_ADD_PERMISSION = "comments.rowComment.create"

#: Permission required to moderate any comment (delete, resolve, tombstones).
COMMENTS_MODERATE_PERMISSION = "comments.rowComment.moderate"

#: Permission required to export comments (Phase 3).
COMMENTS_EXPORT_PERMISSION = "comments.rowComment.export"

APP_NAME = "obox_builder.contrib.comments"

__all__ = [
    "APP_NAME",
    "COMMENTS_ADD_PERMISSION",
    "COMMENTS_EXPORT_PERMISSION",
    "COMMENTS_MODERATE_PERMISSION",
    "COMMENTS_VIEW_PERMISSION",
    "CommentOptions",
]
