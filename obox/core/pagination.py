"""Materialized result windows independent of HTTP and templates."""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement, UnaryExpression

from obox.core.exceptions import ImproperlyConfigured, NotFound

T = TypeVar("T")


@dataclass(frozen=True)
class ListResult(Generic[T]):
    items: Sequence[T]
    offset: int = 0
    limit: int | None = None
    has_more: bool = False
    total: int | None = None

    def __post_init__(self) -> None:
        if (
            type(self.offset) is not int
            or self.offset < 0
            or (self.limit is not None and (type(self.limit) is not int or self.limit < 1))
            or (self.total is not None and (type(self.total) is not int or self.total < 0))
        ):
            raise ValueError("Invalid result window or total.")


@dataclass(frozen=True)
class Page(Generic[T]):
    items: Sequence[T]
    number: int
    page_size: int
    total: int

    def __post_init__(self) -> None:
        if (
            type(self.number) is not int
            or self.number < 1
            or type(self.page_size) is not int
            or self.page_size < 1
            or type(self.total) is not int
            or self.total < 0
        ):
            raise ValueError("Invalid page number, page size or total.")

    @property
    def num_pages(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size)

    @property
    def has_next(self) -> bool:
        return self.number < self.num_pages

    @property
    def has_previous(self) -> bool:
        return self.number > 1

    @property
    def next_number(self) -> int | None:
        return self.number + 1 if self.has_next else None

    @property
    def previous_number(self) -> int | None:
        return self.number - 1 if self.has_previous else None

    @property
    def start_index(self) -> int:
        return (self.number - 1) * self.page_size + 1 if self.items else 0

    @property
    def end_index(self) -> int:
        return self.start_index + len(self.items) - 1 if self.items else 0

    @property
    def is_paginated(self) -> bool:
        return self.num_pages > 1

    def __iter__(self) -> Iterator[T]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)


class Paginator:
    def __init__(self, page_size: int | None) -> None:
        if page_size is not None and (type(page_size) is not int or page_size < 1):
            raise ImproperlyConfigured("Choose a positive page size.")
        self.page_size = page_size

    @staticmethod
    def validate_number(number: int) -> None:
        if type(number) is not int or number < 1:
            raise NotFound("Invalid page.")

    async def paginate(
        self,
        session: AsyncSession,
        stmt: Select[tuple[T]],
        number: int = 1,
        *,
        key: ColumnElement[object] | None = None,
    ) -> Page[T]:
        self.validate_number(number)
        if self.page_size is None:
            if number != 1:
                raise NotFound("Invalid page.")
            items = await self.rows(session, stmt)
            return Page(items, number, max(1, len(items)), len(items))
        total = await self.count(session, stmt, key=key)
        offset = (number - 1) * self.page_size
        if number != 1 and offset >= total:
            raise NotFound("Invalid page.")
        items = await self.rows(session, stmt, key=key, offset=offset, limit=self.page_size)
        return Page(items, number, self.page_size, total)

    @staticmethod
    async def count(
        session: AsyncSession, stmt: Select[tuple[T]], *, key: ColumnElement[object] | None = None
    ) -> int:
        unordered = stmt.order_by(None)
        counted = (
            unordered.with_only_columns(key, maintain_column_froms=True).distinct()
            if key is not None
            else unordered
        )
        return int(await session.scalar(select(func.count()).select_from(counted.subquery())) or 0)

    @staticmethod
    async def rows(
        session: AsyncSession,
        stmt: Select[tuple[T]],
        *,
        key: ColumnElement[object] | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> Sequence[T]:
        if limit is not None or offset:
            if key is None:
                stmt = stmt.offset(offset).limit(limit)
            else:
                # Slice identities so duplicate joined rows do not consume slots.
                columns = []
                for term in stmt._order_by_clauses:
                    while isinstance(term, UnaryExpression):
                        term = term.element
                    columns.append(term)
                identities = (
                    stmt.with_only_columns(key, *columns, maintain_column_froms=True)
                    .distinct()
                    .offset(offset)
                    .limit(limit)
                    .subquery()
                )
                stmt = stmt.where(key.in_(select(identities.c[0])))
        return (await session.scalars(stmt)).unique().all()
