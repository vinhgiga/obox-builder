"""Asynchronous transactions with an atomic context manager."""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from functools import partial
from typing import ParamSpec, cast

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from obox.core.exceptions import ImproperlyConfigured
from obox.db.session import current_session, session_scope

P = ParamSpec("P")
_CALLBACKS = "obox.after_commit"
_Callback = tuple[set[SessionTransaction], Callable[[], object]]


def on_commit(
    session: AsyncSession, fn: Callable[P, object], /, *args: P.args, **kwargs: P.kwargs
) -> None:
    callbacks = cast("list[_Callback] | None", session.info.get(_CALLBACKS))
    if callbacks is None or not session.in_transaction() or not session.is_active:
        raise ImproperlyConfigured("on_commit requires an active unit of work.")
    savepoints = set()
    transaction = session.sync_session.get_nested_transaction()
    while transaction is not None and transaction.nested:
        savepoints.add(transaction)
        transaction = transaction.parent
    callbacks.append((savepoints, partial(fn, *args, **kwargs)))


@contextmanager
def commit_callbacks(session: AsyncSession) -> Iterator[list[_Callback]]:
    callbacks: list[_Callback] = []
    session.info[_CALLBACKS] = callbacks

    def discard_rolled_back(_: Session, transaction: SessionTransaction) -> None:
        if transaction.nested:
            callbacks[:] = [entry for entry in callbacks if transaction not in entry[0]]

    event.listen(session.sync_session, "after_soft_rollback", discard_rolled_back)
    try:
        yield callbacks
    finally:
        session.info.pop(_CALLBACKS)
        event.remove(session.sync_session, "after_soft_rollback", discard_rolled_back)


@asynccontextmanager
async def savepoint_scope(session: AsyncSession) -> AsyncIterator[None]:
    connection = await session.connection()
    if connection.dialect.name == "sqlite":
        raw = await connection.get_raw_connection()
        # Legacy SQLite drivers do not begin a transaction before SAVEPOINT.
        driver = raw.driver_connection
        if driver is not None and not driver.in_transaction:
            await connection.exec_driver_sql("BEGIN")
    async with session.begin_nested():
        yield


@asynccontextmanager
async def atomic() -> AsyncIterator[None]:
    current = current_session.get()
    nested = current is not None and current[0] is asyncio.current_task()
    async with session_scope() as session:
        if nested:
            async with savepoint_scope(session):
                yield
        else:
            yield
