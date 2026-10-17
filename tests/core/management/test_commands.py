from __future__ import annotations

from typing import Any

import pytest
import uvicorn
from obox_builder.conf import SETTINGS_MODULE_ENV
from obox_builder.core.management.cli import create_typer_app
from typer.testing import CliRunner

from tests.support import events

CLI_SETTINGS = "tests.support.cli_settings_core"
CLI_SETTINGS_BAD = "tests.support.cli_settings_bad"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(SETTINGS_MODULE_ENV, CLI_SETTINGS)
    return create_typer_app()


def test_version_command_prints_package_versions(cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["version"])
    assert result.exit_code == 0
    assert "obox-builder" in result.output
    assert "Python" in result.output
    assert "fastapi" in result.output


def test_check_command_reports_no_issues(cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["check"])
    assert result.exit_code == 0
    assert "no issues" in result.output


def test_check_command_reports_errors(cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["check", "--settings", CLI_SETTINGS_BAD])
    assert result.exit_code == 1
    assert "obox.E001" in result.output


def test_check_command_deploy_flag_runs_deploy_checks(cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["check", "--settings", CLI_SETTINGS_BAD, "--deploy"])
    assert result.exit_code == 1
    assert "obox.E002" in result.output
    assert "obox.E003" in result.output


def test_runserver_requires_asgi_application(cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["runserver", "--settings", "tests.support.cli_settings_no_asgi"])
    assert result.exit_code == 1
    assert "ASGI_APPLICATION is not set" in result.output


def test_runserver_refuses_to_start_with_failing_checks(cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["runserver", "--settings", CLI_SETTINGS_BAD])
    assert result.exit_code == 1
    assert "System check identified" in result.output
    assert "obox.E001" in result.output


def test_runserver_starts_uvicorn_with_application(
    cli, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(app: Any, **kwargs: Any) -> None:
        calls.append({"app": app, **kwargs})

    monkeypatch.setattr(uvicorn, "run", fake_run)
    result = runner.invoke(cli, ["runserver", "--host", "127.1.2.3", "--port", "9999"])
    assert result.exit_code == 0
    assert "Starting development server at http://127.1.2.3:9999/" in result.output
    assert len(calls) == 1
    call = calls[0]
    assert call["host"] == "127.1.2.3"
    assert call["port"] == 9999
    assert call["log_level"] == "info"
    assert callable(call["app"])


def test_runserver_verbosity_maps_to_log_level(
    cli, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(app: Any, **kwargs: Any) -> None:
        calls.append({"app": app, **kwargs})

    monkeypatch.setattr(uvicorn, "run", fake_run)
    result = runner.invoke(cli, ["runserver", "-v", "0"])
    assert result.exit_code == 0
    assert calls[0]["log_level"] == "warning"


def test_runserver_reload_uses_import_string(
    cli, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(app: Any, **kwargs: Any) -> None:
        calls.append({"app": app, **kwargs})

    monkeypatch.setattr(uvicorn, "run", fake_run)
    result = runner.invoke(cli, ["runserver", "--reload"])
    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0]["app"] == "tests.support.asgi:application"
    assert calls[0]["reload"] is True


def test_runserver_skip_checks_starts_even_with_errors(
    cli, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(app))
    result = runner.invoke(cli, ["runserver", "--settings", CLI_SETTINGS_BAD, "--skip-checks"])
    assert result.exit_code == 0
    assert len(calls) == 1


def test_runserver_requires_settings(
    cli, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SETTINGS_MODULE_ENV, raising=False)
    result = runner.invoke(cli, ["runserver"])
    assert result.exit_code == 1
    assert events.RECORDS == []


def test_collectstatic_requires_static_root(cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["collectstatic"])
    assert result.exit_code == 1
    assert "STATIC_ROOT is not set" in result.output


def test_collectstatic_copies_from_apps_and_dirs(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv(SETTINGS_MODULE_ENV, "tests.support.templating_settings")
    monkeypatch.setenv("OBOX_STATIC_ROOT", str(tmp_path / "static"))
    result = runner.invoke(create_typer_app(), ["collectstatic"])
    assert result.exit_code == 0, result.output
    assert "Copied 2 file(s)." in result.output
    assert (tmp_path / "static" / "project.txt").exists()
    assert (tmp_path / "static" / "shop" / "logo.txt").exists()


def test_showurls_lists_application_routes(cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["showurls"])
    assert result.exit_code == 0, result.output
    assert "GET" in result.output
    assert "/ping" in result.output
