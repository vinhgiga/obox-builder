from __future__ import annotations

import pytest
from obox_builder.contrib.auth import csrf
from obox_builder.contrib.auth.models import Session as AuthSession
from obox_builder.contrib.auth.services import create_user
from obox_builder.core.exceptions import PermissionDenied
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request


def make_request(
    method: str = "POST",
    *,
    origin: str | None = None,
    referer: str | None = None,
    host: str = "testserver",
    headers: dict[str, str] | None = None,
) -> Request:
    raw_headers: list[tuple[bytes, bytes]] = [(b"host", host.encode())]
    if origin:
        raw_headers.append((b"origin", origin.encode()))
    if referer:
        raw_headers.append((b"referer", referer.encode()))
    for key, value in (headers or {}).items():
        raw_headers.append((key.lower().encode(), value.encode()))
    scope = {
        "type": "http",
        "method": method,
        "path": "/",
        "headers": raw_headers,
        "scheme": "http",
        "server": (host, 80),
        "query_string": b"",
    }
    return Request(scope)


async def test_token_is_generated_once_and_stored_in_session(db: AsyncSession) -> None:
    user = await create_user(db, "alice", "s3cure-password")
    row = AuthSession(user_id=user.id, token_hash="x" * 64, data={})
    first = csrf.get_csrf_token(row)
    second = csrf.get_csrf_token(row)
    assert first == second
    assert row.data[csrf.CSRF_FIELD_NAME] == first
    assert len(first) > 20


async def test_validate_csrf_accepts_matching_token(db: AsyncSession) -> None:
    user = await create_user(db, "alice", "s3cure-password")
    row = AuthSession(user_id=user.id, token_hash="y" * 64, data={})
    token = csrf.get_csrf_token(row)
    request = make_request(origin="http://testserver")
    csrf.validate_csrf(request, row, token)  # does not raise


async def test_validate_csrf_rejects_missing_or_wrong_token(db: AsyncSession) -> None:
    user = await create_user(db, "alice", "s3cure-password")
    row = AuthSession(user_id=user.id, token_hash="z" * 64, data={})
    token = csrf.get_csrf_token(row)
    request = make_request(origin="http://testserver")
    with pytest.raises(PermissionDenied, match="token missing"):
        csrf.validate_csrf(request, row, None)
    with pytest.raises(PermissionDenied, match="token mismatch"):
        csrf.validate_csrf(request, row, token + "x")


async def test_validate_csrf_accepts_header_token(db: AsyncSession) -> None:
    user = await create_user(db, "alice", "s3cure-password")
    row = AuthSession(user_id=user.id, token_hash="w" * 64, data={})
    token = csrf.get_csrf_token(row)
    request = make_request(origin="http://testserver", headers={csrf.CSRF_HEADER_NAME: token})
    csrf.validate_csrf(request, row, None)  # does not raise


def test_check_origin_rejects_other_hosts() -> None:
    assert not csrf.check_origin(make_request(origin="http://evil.example"))
    assert not csrf.check_origin(make_request(referer="http://evil.example/page"))
    assert csrf.check_origin(make_request(origin="http://testserver"))
    assert csrf.check_origin(make_request())  # no origin/referer at all
    assert csrf.check_origin(make_request(method="GET", origin="http://evil.example"))


def test_validate_csrf_rejects_cross_origin() -> None:
    row = AuthSession(user_id=1, token_hash="v" * 64, data={})
    token = csrf.get_csrf_token(row)
    request = make_request(origin="http://evil.example")
    with pytest.raises(PermissionDenied, match="Cross-origin"):
        csrf.validate_csrf(request, row, token)
