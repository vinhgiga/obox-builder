from __future__ import annotations

import os
from collections.abc import Sequence
from typing import ClassVar

import sqlalchemy as sa
from obox_builder.contrib.auth.base_user import AbstractBaseUser
from sqlalchemy.orm import Mapped, mapped_column

if os.environ.get("TEST_INVALID_USER_MODEL") == "NonUniqueAccount":

    class NonUniqueAccount(AbstractBaseUser):
        USERNAME_FIELD: ClassVar[str] = "email"
        email: Mapped[str] = mapped_column(sa.String(254))

elif os.environ.get("TEST_INVALID_USER_MODEL") == "NumericAccount":

    class NumericAccount(AbstractBaseUser):
        USERNAME_FIELD: ClassVar[str] = "employee_number"
        employee_number: Mapped[int] = mapped_column(sa.Integer, unique=True)

elif os.environ.get("TEST_INVALID_USER_MODEL") == "PartialAccount":

    class PartialAccount(AbstractBaseUser):
        USERNAME_FIELD: ClassVar[str] = "email"
        email: Mapped[str] = mapped_column(sa.String(254))
        __table_args__ = (
            sa.Index("partial_email", "email", unique=True, sqlite_where=sa.text("is_active")),
        )

elif os.environ.get("TEST_INVALID_USER_MODEL") == "IndexAccount":

    class IndexAccount(AbstractBaseUser):
        USERNAME_FIELD: ClassVar[str] = "email"
        email: Mapped[str] = mapped_column(sa.String(254))
        __table_args__ = (sa.Index("unique_email", "email", unique=True),)

elif os.environ.get("TEST_INVALID_USER_MODEL") == "AliasAccount":

    class AliasAccount(AbstractBaseUser):
        USERNAME_FIELD: ClassVar[str] = "contact"
        EMAIL_FIELD: ClassVar[str] = "contact"
        contact: Mapped[str] = mapped_column(sa.String(254), unique=True)

elif os.environ.get("TEST_INVALID_USER_MODEL") == "MissingIdentifierAccount":

    class MissingIdentifierAccount(AbstractBaseUser):
        USERNAME_FIELD: ClassVar[str] = "not_a_field"

elif os.environ.get("TEST_INVALID_USER_MODEL") == "InvalidRequiredAccount":

    class InvalidRequiredAccount(AbstractBaseUser):
        USERNAME_FIELD: ClassVar[str] = "email"
        REQUIRED_FIELDS: ClassVar[Sequence[str]] = ["email"]
        email: Mapped[str] = mapped_column(sa.String(254), unique=True)

else:

    class Account(AbstractBaseUser):
        USERNAME_FIELD: ClassVar[str] = "email"
        REQUIRED_FIELDS: ClassVar[Sequence[str]] = ["department"]
        email: Mapped[str] = mapped_column(sa.String(254), unique=True)
        department: Mapped[str] = mapped_column(sa.String(100), default="")
