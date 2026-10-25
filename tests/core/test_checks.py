from __future__ import annotations

from pathlib import Path

import pytest
from obox_builder.conf import BaseSettings, settings
from obox_builder.core.checks import (
    CheckLevel,
    CheckMessage,
    Error,
    Info,
    Warning,
    get_checks,
    register,
    run_checks,
)
from pydantic import SecretStr

SECRET = "x" * 40


def configure_settings(**overrides: object) -> None:
    values: dict[str, object] = {
        "SECRET_KEY": SecretStr(SECRET),
        "DATABASE_URL": "sqlite+aiosqlite://",
    }
    values.update(overrides)
    settings.configure(BaseSettings(**values))  # type: ignore[arg-type]


def ids(messages: list[CheckMessage]) -> set[str | None]:
    return {message.id for message in messages}


def test_message_levels_and_helpers() -> None:
    error = Error("boom", hint="why", id="obox.T001")
    assert error.level is CheckLevel.ERROR
    assert error.is_serious
    assert str(error) == "boom"
    assert error.hint == "why"

    warning = Warning("careful")
    assert warning.level is CheckLevel.WARNING
    assert not warning.is_serious

    info = Info("fyi")
    assert info.level is CheckLevel.INFO

    assert CheckMessage.error("via helper").level is CheckLevel.ERROR
    assert CheckMessage.warning("via helper").level is CheckLevel.WARNING
    assert CheckMessage.info("via helper").level is CheckLevel.INFO
    assert CheckMessage.debug("via helper").level is CheckLevel.DEBUG


def test_register_and_run_custom_check() -> None:
    calls: list[int] = []

    @register(tags=("tests.sample",))
    def sample_check() -> list[CheckMessage]:
        calls.append(1)
        return [Warning("custom sample", id="tests.W001")]

    messages = run_checks(tags=("tests.sample",))
    assert any(message.id == "tests.W001" for message in messages)
    assert calls == [1]


def test_tagged_checks_run_by_default_and_can_be_filtered() -> None:
    configure_settings()

    @register(tags=("tests.hidden",))
    def hidden_check() -> list[CheckMessage]:
        return [Error("tagged check", id="tests.E999")]

    assert "tests.E999" in ids(run_checks())
    assert "tests.E999" in ids(run_checks(tags=("tests.hidden",)))
    assert "tests.E999" not in ids(run_checks(tags=("tests.other",)))


def test_dedupe_when_check_registered_under_multiple_tags() -> None:
    @register(tags=("tests.dup1", "tests.dup2"))
    def dup_check() -> list[CheckMessage]:
        return []

    @register(tags=("tests.dup1",))
    def dup_check_again() -> list[CheckMessage]:
        return []

    register(dup_check_again, tags=("tests.dup2",))

    selected = get_checks(("tests.dup1", "tests.dup2"))
    assert selected.count(dup_check) == 1
    assert selected.count(dup_check_again) == 1


def test_short_secret_key_is_error() -> None:
    configure_settings(SECRET_KEY=SecretStr("short"))
    assert "obox.E001" in ids(run_checks())


def test_long_secret_key_is_ok() -> None:
    configure_settings()
    assert "obox.E001" not in ids(run_checks())


def test_deploy_checks_only_run_with_deploy_flag() -> None:
    configure_settings(DEBUG=True, SECRET_KEY=SecretStr("dev-only-" + "x" * 40))
    assert "obox.E002" not in ids(run_checks())
    assert "obox.E003" not in ids(run_checks())

    deploy_ids = ids(run_checks(deploy=True))
    assert {"obox.E002", "obox.E003"} <= deploy_ids


def test_deploy_clean_config_has_no_serious_messages() -> None:
    configure_settings()
    assert not [message for message in run_checks(deploy=True) if message.is_serious]


def test_empty_installed_apps_warns() -> None:
    configure_settings(INSTALLED_APPS=[])
    assert "obox.W001" in ids(run_checks())


def test_template_dirs_warns_for_missing_directories(tmp_path: Path) -> None:
    configure_settings(TEMPLATE_DIRS=[tmp_path / "nope"])
    assert "obox.W002" in ids(run_checks())

    configure_settings(TEMPLATE_DIRS=[tmp_path])
    assert "obox.W002" not in ids(run_checks())


def test_bad_middleware_entry_is_error() -> None:
    configure_settings(MIDDLEWARE=["does.not.exist.Middleware"])
    assert "obox.E004" in ids(run_checks())


def test_default_middleware_is_importable() -> None:
    configure_settings()
    assert "obox.E004" not in ids(run_checks())


def test_root_urlconf_missing_is_info() -> None:
    configure_settings(ROOT_URLCONF="")
    assert "obox.I001" in ids(run_checks())

    configure_settings(ROOT_URLCONF="config.urls")
    assert "obox.I001" not in ids(run_checks())


def test_session_cookie_secure_warns_on_deploy_only() -> None:
    configure_settings(SESSION_COOKIE_SECURE=False)
    assert "obox.W003" not in ids(run_checks())
    assert "obox.W003" in ids(run_checks(deploy=True))


def test_deploy_checks_flag_unrestricted_or_local_only_hosts() -> None:
    configure_settings(ALLOWED_HOSTS=["*"])
    assert "obox.E006" not in ids(run_checks())
    assert "obox.E006" in ids(run_checks(deploy=True))

    configure_settings(ALLOWED_HOSTS=["localhost", "127.0.0.1"])
    assert "obox.W004" in ids(run_checks(deploy=True))

    configure_settings(ALLOWED_HOSTS=["admin.example.com"])
    assert "obox.E006" not in ids(run_checks(deploy=True))
    assert "obox.W004" not in ids(run_checks(deploy=True))


def test_deploy_checks_report_https_settings() -> None:
    configure_settings()
    deploy_ids = ids(run_checks(deploy=True))
    assert {"obox.W005", "obox.W006"} <= deploy_ids
    assert "obox.W007" not in deploy_ids

    configure_settings(MIDDLEWARE=[])
    assert "obox.W007" in ids(run_checks(deploy=True))

    configure_settings(SECURE_SSL_REDIRECT=True, SECURE_HSTS_SECONDS=31_536_000)
    deploy_ids = ids(run_checks(deploy=True))
    assert "obox.W005" not in deploy_ids
    assert "obox.W006" not in deploy_ids


def test_run_checks_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_settings()
    assert isinstance(run_checks(), list)
