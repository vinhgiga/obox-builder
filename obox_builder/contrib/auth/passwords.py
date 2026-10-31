"""Default password helpers; alternative backends can provide their own."""

from __future__ import annotations

import secrets
from functools import cache
from typing import TYPE_CHECKING

from starlette.concurrency import run_in_threadpool

from obox_builder.errors import BuilderValidationError

if TYPE_CHECKING:
    from pwdlib import PasswordHash


@cache
def _hasher() -> PasswordHash:
    from pwdlib import PasswordHash

    return PasswordHash.recommended()


@cache
def _dummy_hash() -> str:
    return _hasher().hash(secrets.token_urlsafe(32))


async def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 1024:
        raise BuilderValidationError({"password": ["Use between 12 and 1024 characters."]})
    return await run_in_threadpool(lambda: _hasher().hash(password))


async def verify_password(password: str, encoded: str | None) -> bool:
    def verify() -> bool:
        from pwdlib.exceptions import UnknownHashError

        try:
            valid = _hasher().verify(password, encoded if encoded is not None else _dummy_hash())
        except UnknownHashError:
            _hasher().verify(password, _dummy_hash())
            return False
        return valid and encoded is not None

    return await run_in_threadpool(verify)
