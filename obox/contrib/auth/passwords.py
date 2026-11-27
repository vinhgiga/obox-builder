"""Default password helpers; alternative backends can provide their own."""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from functools import cache

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from starlette.concurrency import run_in_threadpool

from obox.contrib.auth.password_validation import PasswordValidator, validate_password
from obox.core.exceptions import BuilderValidationError

_hasher = PasswordHash.recommended()


@cache
def _dummy_hash() -> str:
    return _hasher.hash(secrets.token_urlsafe(32))


async def hash_password(
    password: str,
    *,
    validators: Sequence[PasswordValidator] = (),
    user: object | None = None,
) -> str:
    if not password or len(password) > 1024:
        raise BuilderValidationError({"password": ["Use between 1 and 1024 characters."]})
    validate_password(password, validators, user)
    return await run_in_threadpool(_hasher.hash, password)


async def verify_password(password: str, encoded: str | None) -> bool:
    def verify() -> bool:
        try:
            valid = _hasher.verify(password, encoded if encoded is not None else _dummy_hash())
        except UnknownHashError:
            _hasher.verify(password, _dummy_hash())
            return False
        return valid and encoded is not None

    return await run_in_threadpool(verify)
