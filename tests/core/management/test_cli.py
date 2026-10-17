from __future__ import annotations

import sys
from pathlib import Path

import pytest
from obox_builder.conf import SETTINGS_MODULE_ENV
from obox_builder.core.management import utils
from obox_builder.core.management.base import CommandError
from obox_builder.core.management.cli import create_typer_app
from typer.testing import CliRunner

from tests.support import events

CLI_SETTINGS = "tests.support.cli_settings"
PROBE_MODULE = "tests.support.apps.commands_a.management.commands.probe"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(SETTINGS_MODULE_ENV, CLI_SETTINGS)
    return create_typer_app()


def test_help_lists_commands_without_importing_them(cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for name in ("startproject", "startapp", "version", "check", "runserver", "probe", "greet"):
        assert name in result.output
    assert "Probe command used by the test suite." in result.output
    assert PROBE_MODULE not in sys.modules


def test_running_a_command_imports_its_module(cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["probe"])
    assert result.exit_code == 0
    assert events.RECORDS == ["probe.run"]
    assert PROBE_MODULE in sys.modules


def test_duplicate_name_resolves_to_first_app(cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["shared"])
    assert result.exit_code == 0
    assert events.RECORDS == ["commands_a.shared"]


def test_unknown_command_exits_with_code_2(cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["doesnotexist"])
    assert result.exit_code == 2
    assert "No such command 'doesnotexist'" in result.output


def test_typo_suggests_closest_command(cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["versoin"])
    assert result.exit_code == 2
    assert "Did you mean 'version'?" in result.output


def test_command_arguments_and_options(cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["greet", "vinh", "-n", "3"])
    assert result.exit_code == 0
    assert events.RECORDS == ["greet:vinh:3"]


def test_async_command_is_awaited(cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["async_probe"])
    assert result.exit_code == 0
    assert events.RECORDS == ["async_probe.run"]


def test_requires_settings_runs_setup(cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["needs_settings"])
    assert result.exit_code == 0
    assert "needs_settings.configured=True" in events.RECORDS


def test_core_command_without_settings_fails_cleanly(
    cli, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SETTINGS_MODULE_ENV, raising=False)
    result = runner.invoke(cli, ["runserver"])
    assert result.exit_code == 1
    assert "Settings are not configured" in result.output


def test_settings_option_overrides_environment(
    cli, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SETTINGS_MODULE_ENV, raising=False)
    result = runner.invoke(cli, ["check", "--settings", CLI_SETTINGS])
    assert result.exit_code == 0
    assert "no issues" in result.output


def test_settings_option_rejects_unknown_module(cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["check", "--settings", "does.not.exist"])
    assert result.exit_code == 1
    assert "Could not import settings module 'does.not.exist'" in result.output


def test_verbosity_option_is_applied(cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["probe", "-v", "0"])
    assert result.exit_code == 0
    assert utils.state.verbosity == 0


def test_verbosity_option_is_validated(cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["probe", "-v", "4"])
    assert result.exit_code == 2


def test_pythonpath_option_prepends_sys_path(cli, runner: CliRunner, tmp_path: Path) -> None:
    path = str(tmp_path)
    try:
        result = runner.invoke(cli, ["probe", "--pythonpath", path])
        assert result.exit_code == 0
        assert sys.path[0] == path
    finally:
        sys.path.remove(path)


def test_no_color_option_is_applied(cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["probe", "--no-color"])
    assert result.exit_code == 0
    assert utils.state.no_color is True


def test_command_error_is_reported_without_traceback(cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["fail"])
    assert result.exit_code == 1
    assert "Error: boom" in result.output
    assert "Traceback" not in result.output


def test_traceback_option_reraises_command_error(cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["fail", "--traceback"])
    assert isinstance(result.exception, CommandError)
    assert str(result.exception) == "boom"
