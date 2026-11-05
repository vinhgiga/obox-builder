"""Attachment collections backed by the shared file table."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, declared_attr, foreign, relationship
from sqlalchemy.orm.attributes import flag_modified

from obox_builder.contrib.files.models import File
from obox_builder.contrib.files.rules import Rule


def changed(target: Any, *args: Any) -> None:
    state = sa.inspect(target)
    if state.persistent:
        flag_modified(target, state.mapper.get_property_by_column(state.mapper.primary_key[0]).key)


class _FileList(list[File]):
    owner: Any = None

    def reverse(self) -> None:
        super().reverse()
        changed(self.owner)

    def sort(self, *, key: Any = None, reverse: bool = False) -> None:
        super().sort(key=key, reverse=reverse)
        changed(self.owner)


def initialized(target: Any, collection: _FileList, adapter: Any) -> None:
    collection.owner = target


class _Attachments(declared_attr[list[File]]):
    def __init__(self, rule: Rule) -> None:
        self.name = ""

        def mapping(cls: Any) -> Mapped[list[File]]:
            return relationship(
                File,
                primaryjoin=lambda: sa.and_(
                    foreign(File.owner_id) == sa.cast(sa.inspect(cls).primary_key[0], sa.String),
                    File.owner_type == cls.__table__.fullname,
                    File.field == self.name,
                ),
                viewonly=True,
                collection_class=_FileList,
                lazy="selectin",  # codespell:ignore selectin
                order_by=(File.position, File.id),
                info={"obox_files": rule},
            )

        super().__init__(mapping)

    def __set_name__(self, owner: type[Any], name: str) -> None:
        self.name = name


def attachments(
    *,
    types: Mapping[str, str] | None = None,
    max_size: int | None = None,
    min_count: int = 0,
    max_count: int | None = None,
) -> Mapped[list[File]]:
    """Declare a collection; form/API writes use an ordered list of file IDs."""
    return cast(
        Mapped[list[File]],
        _Attachments(
            Rule(types=types, max_size=max_size, min_count=min_count, max_count=max_count)
        ),
    )
