from __future__ import annotations

import sqlalchemy as sa
from obox_builder.contrib.auth.base_user import AbstractUser
from sqlalchemy.orm import Mapped, mapped_column


class Account(AbstractUser):
    display_name: Mapped[str] = mapped_column(sa.String(100), default="")
