"""Execution state owned by one unit of work."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ParamSpec

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from obox.db.transaction import on_commit

P = ParamSpec("P")


@dataclass
class Context:
    principal: Any
    session: AsyncSession
    request: Request | None = None
    source: str = "job"
    locale: str | None = None
    trace_id: str | None = None
    _owner: Any = field(default=None, repr=False)
    _active: bool = field(default=False, repr=False)
    _failed: bool = field(default=False, repr=False)
    _transaction: Any = field(default=None, repr=False)

    def on_commit(self, fn: Callable[P, object], /, *args: P.args, **kwargs: P.kwargs) -> None:
        """Run a callback after this unit of work commits."""
        on_commit(self.session, fn, *args, **kwargs)
