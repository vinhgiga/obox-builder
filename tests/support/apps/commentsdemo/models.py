from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from obox_builder.db import Model, TimestampMixin
from sqlalchemy.orm import Mapped, mapped_column


class Ticket(Model, TimestampMixin):
    """Commentable model with replies and a custom edit window."""

    __comments__: Any = {"allow_replies": True, "edit_window_seconds": 3600}

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(200))

    def __str__(self) -> str:
        return self.title


class Article(Model, TimestampMixin):
    """Commentable model with replies disabled."""

    __comments__: Any = {"allow_replies": False}

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(200))

    def __str__(self) -> str:
        return self.title


class Legacy(Model):
    """Commentable model that is deliberately not registered in the admin."""

    __comments__ = True

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(200))


class Plain(Model):
    """Not commentable."""

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(200))


class Composite(Model):
    """Commentable model with a composite primary key (E301)."""

    __comments__ = True

    left: Mapped[int] = mapped_column(primary_key=True)
    right: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(200))


class InvalidOptions(Model):
    """Invalid ``__comments__`` declaration (E302)."""

    __comments__: Any = {"unknown": True}

    id: Mapped[int] = mapped_column(primary_key=True)
