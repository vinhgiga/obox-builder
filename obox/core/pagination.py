"""Materialized result windows independent of HTTP and templates."""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class ListResult(Generic[T]):
    items: Sequence[T]
    offset: int = 0
    limit: int | None = None
    has_more: bool = False
    total: int | None = None


@dataclass(frozen=True)
class Page(Generic[T]):
    items: Sequence[T]
    number: int
    page_size: int
    total: int

    @property
    def num_pages(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size)

    @property
    def is_paginated(self) -> bool:
        return self.num_pages > 1

    @property
    def has_next(self) -> bool:
        return self.number < self.num_pages

    @property
    def has_previous(self) -> bool:
        return self.number > 1

    @property
    def next_number(self) -> int | None:
        return self.number + 1 if self.number < self.num_pages else None

    @property
    def previous_number(self) -> int | None:
        return self.number - 1 if self.number > 1 else None

    @property
    def start_index(self) -> int:
        return (self.number - 1) * self.page_size + 1 if self.total else 0

    @property
    def end_index(self) -> int:
        return min(self.number * self.page_size, self.total)

    def __iter__(self) -> Iterator[T]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)
