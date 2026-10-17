from __future__ import annotations

from types import SimpleNamespace

import pytest
from obox_builder.http.messages import add_message, clear_messages, get_and_clear_messages
from starlette.requests import Request


def make_request() -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    request = Request(scope)
    request.state.auth_session = SimpleNamespace(data={})
    return request


async def test_flash_roundtrip() -> None:
    request = make_request()
    await add_message(request, "Saved.", level="success")
    await add_message(request, "Heads up.")
    messages = await get_and_clear_messages(request)
    assert messages == [
        {"level": "success", "message": "Saved."},
        {"level": "info", "message": "Heads up."},
    ]
    assert await get_and_clear_messages(request) == []
    assert request.state.auth_session.data["messages"] == []


async def test_messages_are_isolated_from_other_session_data() -> None:
    request = make_request()
    request.state.auth_session.data = {"csrf": "token"}
    await add_message(request, "One.")
    assert request.state.auth_session.data == {
        "csrf": "token",
        "messages": [{"level": "info", "message": "One."}],
    }


async def test_no_session_is_a_noop() -> None:
    request = make_request()
    request.state.auth_session = None
    await add_message(request, "Dropped.")
    assert await get_and_clear_messages(request) == []


async def test_clear_messages() -> None:
    request = make_request()
    await add_message(request, "Gone.")
    await clear_messages(request)
    assert await get_and_clear_messages(request) == []


async def test_unknown_level_is_rejected() -> None:
    request = make_request()
    with pytest.raises(ValueError, match="Unknown message level"):
        await add_message(request, "Nope.", level="loud")
