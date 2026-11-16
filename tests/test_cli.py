import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from obox.cli import main, startproject


@pytest.mark.parametrize("name", ["bad-name", "class", "../demo"])
def test_startproject_rejects_invalid_names(name: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="valid Python identifier"):
        startproject(name, tmp_path / "project")
    assert not (tmp_path / "project").exists()


def test_scaffold_uses_standard_settings_and_keeps_existing_project_files(tmp_path: Path) -> None:
    marker = tmp_path / "pyproject.toml"
    marker.write_text("# user configuration\n")
    project = startproject("example", tmp_path)
    assert marker.read_text() == "# user configuration\n"
    assert (project / "settings.py").is_file()
    assert (project / "urls.py").is_file()
    assert not (project / "setting.py").exists()
    for path in project.rglob("*.py"):
        compile(path.read_text(), str(path), "exec")
    original = (project / "main.py").read_text()
    with pytest.raises(FileExistsError, match="already exist"):
        startproject("example", tmp_path)
    assert (project / "main.py").read_text() == original


def test_generated_secrets_are_unique_and_template_values_are_replaced(tmp_path: Path) -> None:
    one = startproject("one", tmp_path / "one")
    two = startproject("two", tmp_path / "two")
    settings = [(path / "settings.py").read_text() for path in (one, two)]
    matches = [re.search(r'"OBOX_SECRET_KEY",\s*"([^"]+)"', value) for value in settings]
    assert all(matches)
    keys = [match[1] for match in matches if match is not None]
    assert keys[0] != keys[1]
    assert "__SECRET_KEY__" not in settings[0]
    assert "__PROJECT_NAME__" not in settings[0]
    assert 'PROJECT_NAME = "one"' in settings[0]


def test_command_uses_project_name_and_optional_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["startproject", "project"]) == 0
    assert (tmp_path / "project" / "settings.py").is_file()


def test_generated_project_runs_in_an_isolated_process(tmp_path: Path) -> None:
    project = startproject("example", tmp_path)
    script = """
import asyncio
import httpx
from alembic import command
from alembic.config import Config
from obox.cli import main
command.upgrade(Config("alembic.ini"), "head")
assert main(["createsuperuser", "--username", "admin", "--noinput"]) == 0
from main import app
from obox.conf import settings
assert settings.DATABASES["default"]["ENGINE"] == "obox.db.backends.sqlite3"
async def run():
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/")).status_code == 302
            assert (await client.get("/admin/")).status_code == 302
            assert (await client.get("/auth/login", follow_redirects=True)).status_code == 200
            assert (await client.get("/openapi.json")).status_code == 200
asyncio.run(run())
"""
    subprocess.run(  # noqa: S603 - generated project in isolated test directory
        [sys.executable, "-c", script],
        cwd=project,
        env={
            **os.environ,
            "OBOX_SETTINGS_MODULE": "settings",
            "OBOX_SUPERUSER_PASSWORD": "generated-test-account-value",
        },
        check=True,
        capture_output=True,
        text=True,
    )


def test_generation_failure_preserves_unrelated_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obox import cli

    marker = tmp_path / "existing.txt"
    marker.write_text("keep")
    original = cli._TEMPLATE_FILES.copy()
    monkeypatch.setattr(cli, "_TEMPLATE_FILES", {**original, "bad.txt": "missing-template"})
    with pytest.raises(FileNotFoundError):
        startproject("project", tmp_path)
    assert list(tmp_path.iterdir()) == [marker]
