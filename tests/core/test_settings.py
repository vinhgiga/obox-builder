from __future__ import annotations

import pytest
from obox_builder.conf import SETTINGS_MODULE_ENV, BaseSettings, LazySettings
from obox_builder.core.exceptions import ImproperlyConfigured
from pydantic import SecretStr

SECRET = "s" * 40
DATABASE = "sqlite+aiosqlite://"
SETTINGS_MODULE = "tests.support.settings"


def set_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    module: str | None = SETTINGS_MODULE,
    secret: str | None = SECRET,
    database: str | None = DATABASE,
) -> None:
    if module is None:
        monkeypatch.delenv(SETTINGS_MODULE_ENV, raising=False)
    else:
        monkeypatch.setenv(SETTINGS_MODULE_ENV, module)
    if secret is None:
        monkeypatch.delenv("OBOX_SECRET_KEY", raising=False)
    else:
        monkeypatch.setenv("OBOX_SECRET_KEY", secret)
    if database is None:
        monkeypatch.delenv("OBOX_DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("OBOX_DATABASE_URL", database)


def test_settings_load_lazily_from_module(monkeypatch: pytest.MonkeyPatch) -> None:
    set_environment(monkeypatch)
    lazy = LazySettings()
    assert lazy.configured is False
    assert lazy.ROOT_URLCONF == "config.urls"
    assert lazy.DEBUG is False
    assert lazy.configured is True


def test_load_returns_same_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    set_environment(monkeypatch)
    lazy = LazySettings()
    assert lazy.load() is lazy.load()


def test_missing_settings_module(monkeypatch: pytest.MonkeyPatch) -> None:
    set_environment(monkeypatch, module=None)
    lazy = LazySettings()
    with pytest.raises(ImproperlyConfigured, match=SETTINGS_MODULE_ENV):
        lazy.load()


def test_unimportable_settings_module(monkeypatch: pytest.MonkeyPatch) -> None:
    set_environment(monkeypatch, module="tests.support.nope")
    lazy = LazySettings()
    with pytest.raises(ImproperlyConfigured, match="Could not import settings module"):
        lazy.load()


def test_missing_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    set_environment(monkeypatch, secret=None)
    lazy = LazySettings()
    with pytest.raises(ImproperlyConfigured, match="SECRET_KEY"):
        lazy.load()


def test_unknown_prefixed_env_var_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    set_environment(monkeypatch)
    monkeypatch.setenv("OBOX_NOT_A_FIELD", "x")
    lazy = LazySettings()
    with pytest.raises(ImproperlyConfigured, match="Invalid settings"):
        lazy.load()


def test_env_overrides_and_type_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    set_environment(monkeypatch)
    monkeypatch.setenv("OBOX_DEBUG", "true")
    monkeypatch.setenv("OBOX_SESSION_COOKIE_AGE", "60")
    lazy = LazySettings()
    assert lazy.DEBUG is True
    assert lazy.SESSION_COOKIE_AGE == 60


def test_configure_with_instance() -> None:
    instance = BaseSettings(SECRET_KEY=SecretStr(SECRET), DATABASE_URL=DATABASE)
    lazy = LazySettings()
    lazy.configure(instance)
    assert lazy.load() is instance
    assert lazy.SECRET_KEY.get_secret_value() == SECRET


def test_database_aliases_preserve_legacy_database_url() -> None:
    settings = BaseSettings(
        SECRET_KEY=SecretStr(SECRET),
        DATABASE_URL=DATABASE,
        DATABASES={"analytics": "sqlite+aiosqlite:///analytics.sqlite3"},
    )
    assert settings.database_urls == {
        "default": DATABASE,
        "analytics": "sqlite+aiosqlite:///analytics.sqlite3",
    }


def test_database_alias_can_override_legacy_default_url() -> None:
    settings = BaseSettings(
        SECRET_KEY=SecretStr(SECRET),
        DATABASE_URL=DATABASE,
        DATABASES={"default": "sqlite+aiosqlite:///new-default.sqlite3"},
    )
    assert settings.database_urls["default"] == "sqlite+aiosqlite:///new-default.sqlite3"


def test_configure_with_class() -> None:
    class TestSettings(BaseSettings):
        SECRET_KEY: SecretStr = SecretStr(SECRET)
        DATABASE_URL: str = DATABASE

    lazy = LazySettings()
    lazy.configure(TestSettings)
    assert isinstance(lazy.load(), TestSettings)


def test_configure_with_dotted_path(monkeypatch: pytest.MonkeyPatch) -> None:
    set_environment(monkeypatch)
    lazy = LazySettings()
    lazy.configure(SETTINGS_MODULE)
    assert lazy.configured
    assert lazy.ROOT_URLCONF == "config.urls"


def test_configure_invalid_type() -> None:
    with pytest.raises(TypeError, match="configure"):
        LazySettings().configure(42)  # type: ignore[arg-type]


def test_settings_module_without_settings_class(monkeypatch: pytest.MonkeyPatch) -> None:
    set_environment(monkeypatch, module="tests.support.settings_not_a_class")
    with pytest.raises(ImproperlyConfigured, match="Settings"):
        LazySettings().load()


def test_settings_module_with_multiple_subclasses(monkeypatch: pytest.MonkeyPatch) -> None:
    set_environment(monkeypatch, module="tests.support.settings_ambiguous")
    with pytest.raises(ImproperlyConfigured, match="multiple"):
        LazySettings().load()


def test_switching_configure_replaces_settings() -> None:
    first = BaseSettings(SECRET_KEY=SecretStr(SECRET), DATABASE_URL=DATABASE)
    second = BaseSettings(SECRET_KEY=SecretStr("y" * 40), DATABASE_URL=DATABASE)
    lazy = LazySettings()
    lazy.configure(first)
    lazy.configure(second)
    assert lazy.load() is second


def test_blank_database_url_rejected() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL"):
        BaseSettings(SECRET_KEY=SecretStr(SECRET), DATABASE_URL="  ")
