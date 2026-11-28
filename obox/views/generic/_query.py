from pydantic import BaseModel, Field

from obox.db.filters import FilterGroup


class ViewConfig(BaseModel, frozen=True, extra="forbid"):
    filter: FilterGroup | None = None
    sort: tuple[str, ...] = Field(default=(), max_length=5)
    group: tuple[str, ...] = Field(default=(), max_length=3)
