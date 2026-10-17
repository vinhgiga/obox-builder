"""Password hashing built on pwdlib (Argon2 by default)."""

from __future__ import annotations

import contextlib
from functools import lru_cache

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

# Precomputed Argon2id hash of a throwaway value; verifying against it keeps the
# response time of "unknown user" comparable to "wrong password".
DUMMY_HASH = "$argon2id$v=19$m=65536,t=3,p=4$Om8y5GNd8my/GhkxoPVrsQ$f9e302+H94dBGo2VgR2m66fdgc5vvgquPTxVe5NnQpc"  # noqa: E501

UNUSABLE_PASSWORD_PREFIX = "!"  # noqa: S105 - marker, not a secret


@lru_cache(maxsize=1)
def _hasher() -> PasswordHash:
    return PasswordHash.recommended()


def hash_password(raw_password: str) -> str:
    return _hasher().hash(raw_password)


def make_unusable_password() -> str:
    return UNUSABLE_PASSWORD_PREFIX


def is_usable_password(password_hash: str) -> bool:
    return bool(password_hash) and not password_hash.startswith(UNUSABLE_PASSWORD_PREFIX)


def verify_password(raw_password: str, password_hash: str) -> bool:
    if not is_usable_password(password_hash):
        verify_dummy(raw_password)
        return False
    try:
        return _hasher().verify(raw_password, password_hash)
    except UnknownHashError:
        return False


def verify_and_update(raw_password: str, password_hash: str) -> tuple[bool, str | None]:
    """Verify and re-hash when the stored hash uses outdated parameters."""
    if not is_usable_password(password_hash):
        verify_dummy(raw_password)
        return False, None
    try:
        return _hasher().verify_and_update(raw_password, password_hash)
    except UnknownHashError:
        return False, None


def verify_dummy(raw_password: str) -> None:
    """Burn the same CPU time as a real verification (user enumeration guard)."""
    with contextlib.suppress(UnknownHashError):  # pragma: no cover - constant is valid
        _hasher().verify(raw_password, DUMMY_HASH)
