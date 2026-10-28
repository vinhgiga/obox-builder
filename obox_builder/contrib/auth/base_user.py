"""Abstract base for a project-specific authentication user model."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from obox_builder.db import Model, TimestampMixin, VersionedMixin, relationship
from obox_builder.db.types import JSONB, BigIntIdentity, JsonDict, TZDateTime

if TYPE_CHECKING:
    from obox_builder.contrib.auth.models import Role


class AbstractUser(Model, TimestampMixin, VersionedMixin):
    """Mapped user fields and behavior shared by the built-in and custom users."""

    __abstract__ = True
    __auth_model__ = True
    __history__: ClassVar[bool | Mapping[str, Any] | None] = {
        "exclude": ["meta"],
        "relations": ["roles"],
    }
    permissions: frozenset[str] = frozenset()

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    username: Mapped[str] = mapped_column(sa.String(150), unique=True)
    email: Mapped[str] = mapped_column(sa.String(254), default="")
    password_hash: Mapped[str] = mapped_column(sa.String(255), default="")
    full_name: Mapped[str] = mapped_column(sa.String(255), default="")
    is_active: Mapped[bool] = mapped_column(default=True)
    is_superuser: Mapped[bool] = mapped_column(default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(
        TZDateTime, info={"admin": {"history": False}}
    )
    password_changed_at: Mapped[datetime | None] = mapped_column(
        TZDateTime, info={"admin": {"history": False}}
    )
    # Retain legacy columns so existing databases can still insert users.
    failed_login_count: Mapped[int] = mapped_column(default=0, info={"admin": {"history": False}})
    locked_until: Mapped[datetime | None] = mapped_column(
        TZDateTime, info={"admin": {"history": False}}
    )
    meta: Mapped[JsonDict] = mapped_column(JSONB, default=dict)

    @declared_attr
    def roles(cls) -> Mapped[list[Role]]:  # noqa: N805
        return relationship(
            "obox_builder.contrib.auth.models.Role",
            secondary="auth_user_roles",
            back_populates="users",
        )

    def has_perm(self, perm: str) -> bool:
        """Check an ``app_label.codename`` permission against the loaded set."""
        return self.is_active and (self.is_superuser or perm in self.permissions)

    def __str__(self) -> str:
        return self.username

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.username!r}>"
