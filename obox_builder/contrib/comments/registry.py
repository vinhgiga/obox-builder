"""Which models accept comments, and the options they resolved."""

from __future__ import annotations

from typing import Any

from obox_builder.apps import apps
from obox_builder.conf import settings
from obox_builder.contrib.comments.options import CommentOptions, parse_comments

__all__ = [
    "comments_enabled",
    "declared_models",
    "get_options",
    "is_commentable",
]


def get_options(model: type[Any]) -> CommentOptions:
    """Parse (and validate) the model's current ``__comments__`` declaration."""
    return parse_comments(getattr(model, "__comments__", None))


def is_commentable(model: type[Any]) -> bool:
    """True when the model declares comments and the feature is enabled."""
    return comments_enabled() and get_options(model).enabled


def comments_enabled() -> bool:
    return bool(getattr(settings, "COMMENTS_ENABLED", True))


def declared_models() -> list[tuple[type[Any], CommentOptions]]:
    """Every commentable model currently registered in the app registry."""
    declared: list[tuple[type[Any], CommentOptions]] = []
    for model in apps.get_models():
        options = get_options(model)
        if options.enabled:
            declared.append((model, options))
    return declared
