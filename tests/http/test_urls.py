from __future__ import annotations

# Redirect edge cases are adapted from Django's utils_tests/test_http.py.
import pytest
from obox_builder.http.urls import is_safe_url, safe_next_url


@pytest.mark.parametrize(
    "url",
    [
        "/admin/users",
        "/admin/users?source=https://example.com",
        "/archive\\2026",
    ],
)
def test_same_site_relative_urls_are_safe(url: str) -> None:
    assert is_safe_url(url)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "admin/users",
        "https://evil.example/phish",
        "//evil.example/phish",
        "/\\evil.example/phish",
        "/\\\\evil.example/phish",
        "/\\/evil.example/phish",
        "/\n//evil.example/phish",
        "/" + "a" * 2048,
    ],
)
def test_unsafe_or_ambiguous_urls_are_rejected(url: str | None) -> None:
    assert not is_safe_url(url)


def test_unsafe_next_uses_configured_login_redirect() -> None:
    assert safe_next_url(r"/\evil.example/phish") == "/admin/"
