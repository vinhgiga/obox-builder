"""Authentication and RBAC application (Milestone 4)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obox_builder.contrib.auth.base_user import AbstractBaseUser


def get_user_model() -> type[AbstractBaseUser]:
    """Return the user model selected by ``AUTH_USER_MODEL``."""
    from obox_builder.contrib.auth.models import User

    return User
