"""Execution state owned by one unit of work."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request


@dataclass(frozen=True)
class DomainEvent:
    action: str
    model: type[Any]
    id: Any


@dataclass
class Context:
    principal: Any
    session: AsyncSession
    request: Request | None = None
    source: str = "job"
    tenant: Any = None
    locale: str | None = None
    trace_id: str | None = None
    _owner: Any = field(default=None, repr=False)
    _active: bool = field(default=False, repr=False)
    _failed: bool = field(default=False, repr=False)
    _events: list[DomainEvent] = field(default_factory=list, repr=False)
    _transaction: Any = field(default=None, repr=False)
