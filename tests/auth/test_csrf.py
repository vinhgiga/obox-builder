from __future__ import annotations

# Same-origin and malformed-source cases are adapted from Django's csrf_tests.
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
    scheme: str = "http",
    port: int | None = None,
    headers: dict[str, str] | None = None,
) -> Request:
    port = port if port is not None else (443 if scheme == "https" else 80)
    default_port = 443 if scheme == "https" else 80
    host_header = f"{host}:{port}" if port != default_port else host
    raw_headers: list[tuple[bytes, bytes]] = [(b"host", host_header.encode())]
    if origin is not None:
        raw_headers.append((b"origin", origin.encode()))
    if referer is not None:
        raw_headers.append((b"referer", referer.encode()))
    for key, value in (headers or {}).items():
        raw_headers.append((key.lower().encode(), value.encode()))
    scope = {
        "type": "http",
        "method": method,
        "path": "/",
        "headers": raw_headers,
        "scheme": scheme,
        "server": (host, port),
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
    assert not csrf.check_origin(make_request(scheme="https", referer="https://evil.example/page"))
    assert csrf.check_origin(make_request(origin="http://testserver"))
    assert csrf.check_origin(make_request())  # no origin/referer at all
    # Django doesn't check Referer on HTTP requests when Origin is absent.
    assert csrf.check_origin(make_request(referer="http://evil.example/page"))
    assert csrf.check_origin(make_request(method="GET", origin="http://evil.example"))


@pytest.mark.parametrize(
    ("scheme", "origin"),
    [
        ("http", "https://testserver"),
        ("https", "http://testserver"),
        ("http", "http://testserver:8080"),
        ("http", "http://testserver:bad"),
        ("http", "http://[::1"),
        ("http", "http://testserver/path"),
        ("http", "http://user@testserver"),
        ("http", "null"),
        ("http", ""),
        ("http", "ftp://testserver"),
        ("http", "//testserver"),
        ("http", "http://testserver:65536"),
    ],
)
def test_check_origin_rejects_wrong_scheme_port_or_malformed_origin(
    scheme: str, origin: str
) -> None:
    assert not csrf.check_origin(make_request(scheme=scheme, origin=origin))


def test_check_origin_accepts_case_insensitive_host_and_default_port() -> None:
    request = make_request(scheme="https", origin="HTTPS://TESTSERVER:443")
    assert csrf.check_origin(request)


def test_check_origin_matches_explicit_request_port() -> None:
    assert csrf.check_origin(make_request(port=8080, origin="http://testserver:8080"))
    assert not csrf.check_origin(make_request(port=8080, origin="http://testserver"))


def test_check_origin_handles_ipv6_authority() -> None:
    request = make_request(host="[::1]", origin="http://[::1]")
    assert csrf.check_origin(request)


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "TRACE"])
def test_safe_methods_skip_origin_check(method: str) -> None:
    assert csrf.check_origin(make_request(method, origin="http://evil.example"))


def test_https_without_origin_requires_same_origin_referer() -> None:
    assert not csrf.check_origin(make_request(scheme="https"))
    assert csrf.check_origin(make_request(scheme="https", referer="https://testserver/admin/form"))
    assert not csrf.check_origin(
        make_request(scheme="https", referer="http://testserver/admin/form")
    )


def test_validate_csrf_requires_https_referer_when_origin_is_missing() -> None:
    row = AuthSession(user_id=1, token_hash="r" * 64, data={})
    token = csrf.get_csrf_token(row)
    with pytest.raises(PermissionDenied, match="Cross-origin"):
        csrf.validate_csrf(make_request(scheme="https"), row, token)
    csrf.validate_csrf(
        make_request(scheme="https", referer="https://testserver/admin/form"),
        row,
        token,
    )


def test_validate_csrf_rejects_cross_origin() -> None:
    row = AuthSession(user_id=1, token_hash="v" * 64, data={})
    token = csrf.get_csrf_token(row)
    request = make_request(origin="http://evil.example")
    with pytest.raises(PermissionDenied, match="Cross-origin"):
        csrf.validate_csrf(request, row, token)
