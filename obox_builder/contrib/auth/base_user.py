"""Abstract base for a project-specific authentication user model."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

import sqlalchemy as sa
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from obox_builder.db import Model, TimestampMixin, VersionedMixin, relationship
from obox_builder.db.types import JSONB, BigIntIdentity, JsonDict, TZDateTime

if TYPE_CHECKING:
    from obox_builder.contrib.auth.models import Role


class AbstractBaseUser(Model, TimestampMixin, VersionedMixin):
    """Authentication and permission fields for a custom user model."""

    __abstract__ = True
    __auth_model__ = True
    __history__: ClassVar[bool | Mapping[str, Any] | None] = {"relations": ["roles"]}
    permissions: frozenset[str] = frozenset()
    USERNAME_FIELD: ClassVar[str] = "username"
    EMAIL_FIELD: ClassVar[str] = "email"
    REQUIRED_FIELDS: ClassVar[Sequence[str]] = ()

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    password_hash: Mapped[str] = mapped_column(sa.String(255), default="")
    is_active: Mapped[bool] = mapped_column(default=True)
    is_superuser: Mapped[bool] = mapped_column(default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(
        TZDateTime, info={"admin": {"history": False}}
    )
    password_changed_at: Mapped[datetime | None] = mapped_column(
        TZDateTime, info={"admin": {"history": False}}
    )

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

    def get_username(self) -> str:
        value = getattr(self, self.USERNAME_FIELD)
        return "" if value is None else str(value)

    @classmethod
    def get_email_field_name(cls) -> str:
        return cls.EMAIL_FIELD

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_anonymous(self) -> bool:
        return False

    @hybrid_property
    def username(self) -> str:
        return self.get_username()

    @username.inplace.expression
    @classmethod
    def _username_expression(cls) -> Any:
        identifier = getattr(cls, cls.USERNAME_FIELD)
        column = cls.__table__.columns[cls.USERNAME_FIELD]
        return identifier if isinstance(column.type, sa.String) else sa.cast(identifier, sa.String)

    def __str__(self) -> str:
        return self.get_username()

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.get_username()!r}>"


class AbstractUser(AbstractBaseUser):
    """Default user fields for applications that need the standard profile."""

    __abstract__ = True
    __history__: ClassVar[bool | Mapping[str, Any] | None] = {
        "exclude": ["meta"],
        "relations": ["roles"],
    }

    username: Mapped[str] = mapped_column(  # pyright: ignore[reportIncompatibleVariableOverride]
        sa.String(150), unique=True
    )
    email: Mapped[str] = mapped_column(sa.String(254), default="")
    full_name: Mapped[str] = mapped_column(sa.String(255), default="")
    # Retain legacy columns so existing databases can still insert users.
    failed_login_count: Mapped[int] = mapped_column(default=0, info={"admin": {"history": False}})
    locked_until: Mapped[datetime | None] = mapped_column(
        TZDateTime, info={"admin": {"history": False}}
    )
    meta: Mapped[JsonDict] = mapped_column(JSONB, default=dict)
