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
    def has_next(self) -> bool:
        return self.number < self.num_pages

    @property
    def has_previous(self) -> bool:
        return self.number > 1

    def __iter__(self) -> Iterator[T]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)
