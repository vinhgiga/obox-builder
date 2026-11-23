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

    def __iter__(self) -> Iterator[T]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)
