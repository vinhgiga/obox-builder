from __future__ import annotations

from pathlib import Path

import pytest
from obox_builder.contrib.admin.management.commands import tailwind as tailwind_command
from obox_builder.contrib.admin.management.commands.tailwind import (
    BASE_CSS,
    build_input_css,
    template_directories,
)
from typer.testing import CliRunner

from tests.admin.conftest import build_app  # noqa: F401 - ensures settings import order


@pytest.fixture
def cli(admin_env: None):
    from obox_builder.core.management.cli import create_typer_app

    return create_typer_app()


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_input_css_has_prefix_and_no_preflight(admin_env: None) -> None:
    css = build_input_css(template_directories())
    assert "prefix(tw)" in css
    assert "preflight" not in css
    assert "obs" not in BASE_CSS
    assert css.count("@source") == len(template_directories())
    assert 'obox_builder/contrib/admin/templates"' in css


def test_tailwind_build_reports_missing_dependency(
    cli, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tailwind_command, "_find_spec", lambda name: None)
    result = runner.invoke(cli, ["tailwind", "build", "-o", str(tmp_path / "tw.css")])
    assert result.exit_code == 1
    assert "obox-builder[tailwind]" in result.output


def test_tailwind_rejects_unknown_action(admin_env: None, cli, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["tailwind", "explode"])
    assert result.exit_code != 0
    assert "Unknown action" in result.output
