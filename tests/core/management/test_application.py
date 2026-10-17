from __future__ import annotations

from typing import Any

import obox_builder
import pytest
from fastapi.testclient import TestClient
from obox_builder.apps import apps
from obox_builder.conf import SETTINGS_MODULE_ENV, BaseSettings
from obox_builder.core.application import create_app, get_asgi_application
from obox_builder.core.exceptions import ImproperlyConfigured
from pydantic import SecretStr

CLI_SETTINGS = "tests.support.cli_settings"


def _configure(**overrides: Any) -> None:
    values: dict[str, Any] = {
        "SECRET_KEY": SecretStr("x" * 40),
        "DATABASE_URL": "sqlite+aiosqlite://",
    }
    values.update(overrides)
    obox_builder.configure(BaseSettings(**values))


def test_create_app_serves_router_from_root_urlconf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SETTINGS_MODULE_ENV, CLI_SETTINGS)
    client = TestClient(create_app())
    response = client.get("/ping")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_app_applies_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SETTINGS_MODULE_ENV, CLI_SETTINGS)
    client = TestClient(create_app())
    response = client.get("/ping")
    assert response.headers["x-request-id"]


def test_create_app_without_root_urlconf() -> None:
    _configure(ROOT_URLCONF="")
    application = create_app()
    assert application.routes


def test_create_app_rejects_missing_root_urlconf_module() -> None:
    _configure(ROOT_URLCONF="tests.support.does_not_exist")
    with pytest.raises(ImproperlyConfigured, match="Could not import ROOT_URLCONF"):
        create_app()


def test_create_app_rejects_malformed_router_entries() -> None:
    _configure(ROOT_URLCONF="tests.support.urls_broken")
    with pytest.raises(ImproperlyConfigured, match="must be APIRouter"):
        create_app()


def test_create_app_rejects_middleware_that_is_not_a_class() -> None:
    _configure(MIDDLEWARE=["tests.support.urls.router"])
    with pytest.raises(ImproperlyConfigured, match="must point to a class"):
        create_app()


def test_get_asgi_application_sets_up_framework(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SETTINGS_MODULE_ENV, CLI_SETTINGS)
    application = get_asgi_application()
    assert apps.ready
    client = TestClient(application)
    assert client.get("/ping").status_code == 200


def test_get_asgi_application_is_usable_as_asgi_callable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SETTINGS_MODULE_ENV, CLI_SETTINGS)
    application = get_asgi_application()
    assert callable(application)
    response = TestClient(application).get("/ping")
    assert response.json() == {"status": "ok"}


def test_default_middleware_adds_security_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SETTINGS_MODULE_ENV, CLI_SETTINGS)
    response = TestClient(create_app()).get("/ping")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["x-frame-options"] == "DENY"


def test_create_app_mounts_static_files_in_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SETTINGS_MODULE_ENV, "tests.support.templating_settings")
    client = TestClient(get_asgi_application())
    assert client.get("/static/shop/logo.txt").text == "shop-logo\n"
    assert client.get("/static/project.txt").status_code == 200
    assert client.get("/static/nope.txt").status_code == 404
