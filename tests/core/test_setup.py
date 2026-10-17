from __future__ import annotations

import obox_builder
import pytest
from obox_builder.apps import apps
from obox_builder.conf import SETTINGS_MODULE_ENV
from pydantic import SecretStr

SETTINGS_MODULE = "tests.support.settings"


def set_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SETTINGS_MODULE_ENV, SETTINGS_MODULE)
    monkeypatch.setenv("OBOX_SECRET_KEY", "x" * 40)
    monkeypatch.setenv("OBOX_DATABASE_URL", "sqlite+aiosqlite://")


def test_setup_loads_settings_and_populates_apps(monkeypatch: pytest.MonkeyPatch) -> None:
    set_environment(monkeypatch)
    config = obox_builder.setup()
    assert config is obox_builder.settings.load()
    assert apps.apps_ready
    assert apps.models_ready
    assert apps.ready


def test_setup_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    set_environment(monkeypatch)
    first = obox_builder.setup()
    second = obox_builder.setup()
    assert first is second


def test_configure_sets_proxy() -> None:
    instance = obox_builder.BaseSettings(
        SECRET_KEY=SecretStr("x" * 40), DATABASE_URL="sqlite+aiosqlite://"
    )
    obox_builder.configure(instance)
    assert obox_builder.settings.load() is instance


def test_reset_allows_setup_again(monkeypatch: pytest.MonkeyPatch) -> None:
    set_environment(monkeypatch)
    first = obox_builder.setup()
    obox_builder._reset()
    second = obox_builder.setup()
    assert first is second
