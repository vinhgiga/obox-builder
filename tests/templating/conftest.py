from __future__ import annotations

import obox_builder
import pytest
from obox_builder.conf import SETTINGS_MODULE_ENV
from starlette.requests import Request

SETTINGS_MODULE = "tests.support.templating_settings"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SETTINGS_MODULE_ENV, SETTINGS_MODULE)
    obox_builder.setup()


@pytest.fixture
def http_request() -> Request:
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
    return Request(scope)
