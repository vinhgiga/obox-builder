from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from obox_builder.core.management.cli import create_typer_app
from typer.testing import CliRunner

EXPECTED_PROJECT_FILES = [
    "manage.py",
    "pyproject.toml",
    "README.md",
    ".env.example",
    "config/__init__.py",
    "config/settings.py",
    "config/urls.py",
    "config/asgi.py",
    "apps/__init__.py",
    "migrations/env.py",
    "migrations/script.py.mako",
    "migrations/versions/.gitkeep",
    "templates/.gitkeep",
    "static/.gitkeep",
]

EXPECTED_APP_FILES = [
    "__init__.py",
    "apps.py",
    "models.py",
    "urls.py",
    "views.py",
    "management/__init__.py",
    "management/commands/__init__.py",
    "tests/__init__.py",
    "tests/test_smoke.py",
]


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _run(runner: CliRunner, args: list[str]) -> Any:
    result = runner.invoke(create_typer_app(), args)
    assert result.exit_code == 0, result.output
    return result


def _assert_compiles(root: Path) -> None:
    for path in root.rglob("*.py"):
        compile(path.read_text(), str(path), "exec")


def test_startproject_creates_expected_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    monkeypatch.chdir(tmp_path)
    result = _run(runner, ["startproject", "myproj"])
    assert "Created project 'myproj'" in result.output
    project = tmp_path / "myproj"
    for relative in EXPECTED_PROJECT_FILES:
        assert (project / relative).is_file(), relative
    assert not list(project.rglob("*-tpl"))
    _assert_compiles(project)


def test_startproject_generates_random_secret_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    monkeypatch.chdir(tmp_path)
    _run(runner, ["startproject", "myproj"])
    text = (tmp_path / "myproj" / "config" / "settings.py").read_text()
    assert "$" not in text
    keys = re.findall(r'SecretStr\("([^"]+)"\)', text)
    assert keys
    assert len(keys[0]) >= 32


def test_startproject_uses_distinct_secret_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    monkeypatch.chdir(tmp_path)
    _run(runner, ["startproject", "alpha"])
    _run(runner, ["startproject", "beta"])
    pattern = re.compile(r'SecretStr\("([^"]+)"\)')
    alpha_match = pattern.search((tmp_path / "alpha" / "config" / "settings.py").read_text())
    beta_match = pattern.search((tmp_path / "beta" / "config" / "settings.py").read_text())
    assert alpha_match is not None
    assert beta_match is not None
    assert alpha_match.group(1) != beta_match.group(1)


def test_startproject_custom_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "nested" / "place"
    _run(runner, ["startproject", "myproj", str(target)])
    assert (target / "manage.py").is_file()


def test_startproject_rejects_existing_non_empty_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "myproj"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    result = runner.invoke(create_typer_app(), ["startproject", "myproj"])
    assert result.exit_code == 1
    assert "already exists" in result.output
    assert (target / "keep.txt").read_text() == "keep"


def test_startproject_rejects_invalid_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in ("my-proj", "2proj", "my.proj", "class"):
        result = runner.invoke(create_typer_app(), ["startproject", name])
        assert result.exit_code == 1, name


def test_startapp_creates_expected_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apps").mkdir()
    (tmp_path / "apps" / "__init__.py").write_text("")
    _run(runner, ["startapp", "blog", "apps/blog"])
    app_dir = tmp_path / "apps" / "blog"
    for relative in EXPECTED_APP_FILES:
        assert (app_dir / relative).is_file(), relative
    assert not list(app_dir.rglob("*-tpl"))
    _assert_compiles(tmp_path / "apps")


def test_startapp_renders_app_class_and_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apps").mkdir()
    (tmp_path / "apps" / "__init__.py").write_text("")
    _run(runner, ["startapp", "blog", "apps/blog"])
    app_dir = tmp_path / "apps" / "blog"
    apps_py = (app_dir / "apps.py").read_text()
    assert "class BlogConfig(AppConfig):" in apps_py
    assert 'name = "apps.blog"' in apps_py
    smoke = (app_dir / "tests" / "test_smoke.py").read_text()
    assert "from apps.blog.apps import BlogConfig" in smoke
    urls_py = (app_dir / "urls.py").read_text()
    assert "blog" in urls_py


def test_startapp_defaults_to_package_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    monkeypatch.chdir(tmp_path)
    _run(runner, ["startapp", "blog"])
    assert (tmp_path / "blog" / "apps.py").is_file()


def test_generated_project_manage_py_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    monkeypatch.chdir(tmp_path)
    _run(runner, ["startproject", "myproj"])
    project = tmp_path / "myproj"
    env = {key: value for key, value in os.environ.items() if not key.startswith("OBOX_")}
    version = subprocess.run(
        [sys.executable, "manage.py", "version"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert version.returncode == 0, version.stderr
    assert "obox-builder" in version.stdout
    check = subprocess.run(
        [sys.executable, "manage.py", "check"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert check.returncode == 0, check.stderr
    assert "no issues" in check.stdout
