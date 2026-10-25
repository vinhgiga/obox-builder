"""Template context: ``user`` and the Django-style ``perms`` wrapper."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from obox_builder.contrib.auth.models import (
    USER_MODEL_NAME,
    AnonymousUser,
    User,
)


class PermLookup:
    """``perms.blog.add_post`` lookups within one app label."""

    def __init__(self, user: User | AnonymousUser, app_label: str) -> None:
        self._user = user
        self._app_label = app_label

    def __getattr__(self, name: str) -> bool:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._user.has_perm(f"{self._app_label}.{name}")

    def __contains__(self, name: str) -> bool:
        return self._user.has_perm(f"{self._app_label}.{name}")

    def __getitem__(self, name: str) -> bool:
        return self._user.has_perm(f"{self._app_label}.{name}")


class PermWrapper:
    """``perms.app_label`` attribute lookups for templates and views.

    Delegates to ``user.has_perm`` so superusers (whose stored permission set
    is empty but who bypass checks) are reported correctly.
    """

    def __init__(self, user: User | AnonymousUser) -> None:
        self._user = user

    def __getattr__(self, name: str) -> PermLookup:
        if name.startswith("_"):
            raise AttributeError(name)
        return PermLookup(self._user, name)

    def __contains__(self, perm: str) -> bool:
        return self._user.has_perm(perm)


def get_request_user(request: Request) -> User | AnonymousUser:
    return getattr(request.state, "user", None) or AnonymousUser()


def auth(request: Request) -> dict[str, Any]:
    """Context processor exposing ``user`` and ``perms`` to templates."""
    user = get_request_user(request)
    return {
        "user": user,
        "perms": PermWrapper(user),
        "user_model_name": USER_MODEL_NAME,
        "user_admin_permissions": PermLookup(user, User._model_label),
    }
