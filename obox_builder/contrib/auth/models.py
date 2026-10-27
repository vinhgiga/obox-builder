"""User, Role, Permission and Session models (headless auth app)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, ClassVar

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from obox_builder.apps import apps
from obox_builder.conf import settings
from obox_builder.contrib.auth.base_user import AbstractUser
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.db import Model, TimestampMixin, VersionedMixin, relationship
from obox_builder.db.base import ModelBase
from obox_builder.db.types import JSONB, BigIntIdentity, JsonDict, TZDateTime
from obox_builder.utils.strings import camel_to_snake

USER_MODEL = settings.AUTH_USER_MODEL if settings.configured else "auth.User"


def _resolve_user_model() -> type[AbstractUser]:
    app_label, _, model_name = USER_MODEL.partition(".")
    try:
        app_config = apps.get_app_config(app_label)
    except LookupError as exc:
        raise ImproperlyConfigured(
            f"AUTH_USER_MODEL refers to app {app_label!r}, which is not installed."
        ) from exc
    app_config.import_models()
    model = apps.all_models.get(app_label, {}).get(camel_to_snake(model_name))
    if model is None:
        raise ImproperlyConfigured(f"AUTH_USER_MODEL refers to missing model {USER_MODEL!r}.")
    if not issubclass(model, AbstractUser):
        raise ImproperlyConfigured(f"AUTH_USER_MODEL {USER_MODEL!r} must subclass AbstractUser.")
    return model


def _build_builtin_user() -> type[AbstractUser]:
    """Define the default model only when selected by settings."""

    class User(AbstractUser):
        """Built-in user model, used unless AUTH_USER_MODEL selects another model."""

    return User


if TYPE_CHECKING:
    User = AbstractUser
else:
    User = _build_builtin_user() if USER_MODEL == "auth.User" else _resolve_user_model()

USER_MODEL_NAME = camel_to_snake(User.__name__)
USER_VIEW_PERMISSION = f"{User._model_label}.view_{USER_MODEL_NAME}"
USER_ADD_PERMISSION = f"{User._model_label}.add_{USER_MODEL_NAME}"
USER_CHANGE_PERMISSION = f"{User._model_label}.change_{USER_MODEL_NAME}"


user_roles = sa.Table(
    "auth_user_roles",
    ModelBase.metadata,
    sa.Column(
        "user_id",
        sa.ForeignKey(f"{User.__tablename__}.id", ondelete="CASCADE"),
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


class Role(Model, TimestampMixin, VersionedMixin):
    """Roles are assignable identities, so they are tracked as well."""

    __history__ = True

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(150), unique=True)
    description: Mapped[str] = mapped_column(sa.Text, default="")
    is_system: Mapped[bool] = mapped_column(default=False)

    users: Mapped[list[AbstractUser]] = relationship(
        User, secondary=user_roles, back_populates="roles"
    )
    permissions: Mapped[list[Permission]] = relationship(
        secondary=role_permissions, back_populates="roles"
    )

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"<Role {self.name!r}>"


class Permission(Model, TimestampMixin):
    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    app_label: Mapped[str] = mapped_column(sa.String(100))  # type: ignore[misc]
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
        sa.ForeignKey(f"{User.__tablename__}.id", ondelete="CASCADE"), index=True
    )
    data: Mapped[JsonDict] = mapped_column(JSONB, default=dict)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(TZDateTime)
    ip: Mapped[str | None] = mapped_column(sa.String(45))
    user_agent: Mapped[str | None] = mapped_column(sa.String(255))

    user: Mapped[AbstractUser] = relationship(User)

    def __repr__(self) -> str:
        return f"<Session user_id={self.user_id!r}>"


class PasswordResetToken(Model, TimestampMixin):
    """A hashed, expiring, one-use password reset credential."""

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        sa.ForeignKey(f"{User.__tablename__}.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, index=True)
    used_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)


class ApiToken(Model, TimestampMixin, VersionedMixin):
    __history__ = {"exclude": ["token_hash"]}

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        sa.ForeignKey(f"{User.__tablename__}.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(sa.String(100), index=True)
    token_prefix: Mapped[str] = mapped_column(sa.String(16), index=True)
    token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True)
    scopes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime, index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(
        TZDateTime, info={"admin": {"history": False}}
    )
    last_used_ip: Mapped[str | None] = mapped_column(
        sa.String(45), info={"admin": {"history": False}}
    )
    revoked_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    __table_args__ = (sa.CheckConstraint("length(name) BETWEEN 1 AND 100", name="name_length"),)

    def __str__(self) -> str:
        return self.name


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
