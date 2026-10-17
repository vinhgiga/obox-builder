"""User, Role, Permission and Session models (headless auth app)."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from obox_builder.db import Model, TimestampMixin, relationship
from obox_builder.db.base import ModelBase
from obox_builder.db.types import JSONB, BigIntIdentity, JsonDict, TZDateTime

user_roles = sa.Table(
    "auth_user_roles",
    ModelBase.metadata,
    sa.Column(
        "user_id",
        sa.ForeignKey("auth_user.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "role_id",
        sa.ForeignKey("auth_role.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

role_permissions = sa.Table(
    "auth_role_permissions",
    ModelBase.metadata,
    sa.Column(
        "role_id",
        sa.ForeignKey("auth_role.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "permission_id",
        sa.ForeignKey("auth_permission.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class User(Model, TimestampMixin):
    # Effective permissions loaded per request (deny-by-default when not loaded).
    permissions: frozenset[str] = frozenset()

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    username: Mapped[str] = mapped_column(sa.String(150), unique=True)
    email: Mapped[str] = mapped_column(sa.String(254), default="")
    password_hash: Mapped[str] = mapped_column(sa.String(255), default="")
    full_name: Mapped[str] = mapped_column(sa.String(255), default="")
    is_active: Mapped[bool] = mapped_column(default=True)
    is_superuser: Mapped[bool] = mapped_column(default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    password_changed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    failed_login_count: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None] = mapped_column(TZDateTime)
    meta: Mapped[JsonDict] = mapped_column(JSONB, default=dict)

    roles: Mapped[list[Role]] = relationship(secondary=user_roles, back_populates="users")

    def has_perm(self, perm: str) -> bool:
        """Check an ``app_label.codename`` permission against the loaded set."""
        return self.is_superuser or perm in self.permissions

    @property
    def is_locked(self) -> bool:
        return self.locked_until is not None

    def __str__(self) -> str:
        return self.username

    def __repr__(self) -> str:
        return f"<User {self.username!r}>"


class Role(Model, TimestampMixin):
    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(150), unique=True)
    description: Mapped[str] = mapped_column(sa.Text, default="")
    is_system: Mapped[bool] = mapped_column(default=False)

    users: Mapped[list[User]] = relationship(secondary=user_roles, back_populates="roles")
    permissions: Mapped[list[Permission]] = relationship(
        secondary=role_permissions, back_populates="roles"
    )

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"<Role {self.name!r}>"


class Permission(Model, TimestampMixin):
    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    app_label: Mapped[str] = mapped_column(sa.String(100))  # pyright: ignore[reportIncompatibleVariableOverride]
    codename: Mapped[str] = mapped_column(sa.String(100))
    name: Mapped[str] = mapped_column(sa.String(255))
    model: Mapped[str | None] = mapped_column(sa.String(100))

    roles: Mapped[list[Role]] = relationship(
        secondary=role_permissions, back_populates="permissions"
    )

    __table_args__ = (sa.UniqueConstraint("app_label", "codename"),)

    @property
    def full_name(self) -> str:
        return f"{self.app_label}.{self.codename}"

    def __str__(self) -> str:
        return self.full_name

    def __repr__(self) -> str:
        return f"<Permission {self.full_name!r}>"


class Session(Model, TimestampMixin):
    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True)
    user_id: Mapped[int] = mapped_column(
        sa.ForeignKey("auth_user.id", ondelete="CASCADE"), index=True
    )
    data: Mapped[JsonDict] = mapped_column(JSONB, default=dict)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(TZDateTime)
    ip: Mapped[str | None] = mapped_column(sa.String(45))
    user_agent: Mapped[str | None] = mapped_column(sa.String(255))

    user: Mapped[User] = relationship()

    def __repr__(self) -> str:
        return f"<Session user_id={self.user_id!r}>"


class AnonymousUser:
    """Stand-in used by templates and dependency defaults for anonymous requests."""

    id: None = None
    username = ""
    full_name = ""
    is_active = False
    is_superuser = False
    is_authenticated = False
    permissions: ClassVar[frozenset[str]] = frozenset()

    def has_perm(self, perm: str) -> bool:
        return False

    def __str__(self) -> str:
        return "AnonymousUser"
