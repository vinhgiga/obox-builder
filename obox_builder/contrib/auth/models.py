"""Optional auth tables; migrate Base.metadata with the host application."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


user_roles = sa.Table(
    "obox_auth_user_roles",
    Base.metadata,
    sa.Column("user_id", sa.ForeignKey("obox_auth_users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("role_id", sa.ForeignKey("obox_auth_roles.id", ondelete="CASCADE"), primary_key=True),
)


class RoleMixin:
    name: Mapped[str] = mapped_column(sa.String(150), unique=True)
    permissions: Mapped[list[str]] = mapped_column(MutableList.as_mutable(sa.JSON), default=list)


class UserMixin:
    USERNAME_FIELD: ClassVar[str] = "username"

    email: Mapped[str] = mapped_column(sa.String(254), default="")
    first_name: Mapped[str] = mapped_column(sa.String(150), default="")
    last_name: Mapped[str] = mapped_column(sa.String(150), default="")
    department: Mapped[str] = mapped_column(sa.String(150), default="")
    password_hash: Mapped[str] = mapped_column(sa.String(255))
    is_active: Mapped[bool] = mapped_column(default=True)
    is_superuser: Mapped[bool] = mapped_column(default=False)


class SessionMixin:
    token_hash: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    auth_hash: Mapped[str] = mapped_column(sa.String(64))
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), index=True)


class APITokenMixin:
    token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True)
    name: Mapped[str] = mapped_column(sa.String(150))
    scopes: Mapped[list[str]] = mapped_column(sa.JSON)
    auth_hash: Mapped[str] = mapped_column(sa.String(64))
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class Role(RoleMixin, Base):
    __tablename__ = "obox_auth_roles"

    id: Mapped[int] = mapped_column(primary_key=True)


class User(UserMixin, Base):
    __tablename__ = "obox_auth_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(sa.String(150), unique=True)
    roles: Mapped[list[Role]] = relationship(secondary=user_roles, lazy="raise")


class Session(SessionMixin, Base):
    __tablename__ = "obox_auth_sessions"

    user_id: Mapped[int] = mapped_column(sa.ForeignKey(User.id, ondelete="CASCADE"), index=True)


class APIToken(APITokenMixin, Base):
    __tablename__ = "obox_auth_api_tokens"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[int] = mapped_column(sa.ForeignKey(User.id, ondelete="CASCADE"), index=True)
