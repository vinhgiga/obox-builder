from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from obox_builder import cli
from obox_builder.cli import main, startproject

PROJECT_FILES = {
    ".editorconfig",
    "alembic.ini",
    "justfile",
    "main.py",
    "migrations",
    "models.py",
    "setting.py",
}


def test_startproject_creates_runnable_skeleton(tmp_path: Path) -> None:
    project = startproject("demo", tmp_path / "demo")

    assert project == tmp_path / "demo"
    assert {path.name for path in project.iterdir()} == PROJECT_FILES
    assert {path.name for path in (project / "migrations").iterdir()} == {
        "env.py",
        "script.py.mako",
        "versions",
    }
    assert (project / "migrations" / "versions" / "__init__.py").is_file()
    editorconfig = (project / ".editorconfig").read_text()
    assert "[*.html]\nindent_style = space\nindent_size = 2" in editorconfig
    justfile = (project / "justfile").read_text()
    assert justfile == "dev:\n    uv run uvicorn main:app --reload\n"
    settings = (project / "setting.py").read_text()
    assert 'PROJECT_NAME = "demo"' in settings
    assert "# SECURITY WARNING: keep the secret key used in production secret!" in settings
    assert 'SECRET_KEY = os.getenv("OBOX_SECRET_KEY", "' in settings
    assert "__SECRET_KEY__" not in settings
    compile((project / "main.py").read_text(), str(project / "main.py"), "exec")


def test_startproject_generates_a_unique_secret_per_project(tmp_path: Path) -> None:
    first = startproject("first", tmp_path / "first")
    second = startproject("second", tmp_path / "second")

    def configured_secret(project: Path) -> str:
        line = next(
            line
            for line in (project / "setting.py").read_text().splitlines()
            if line.startswith("SECRET_KEY =")
        )
        return line.rsplit('"', 2)[1]

    first_key = configured_secret(first)
    second_key = configured_secret(second)
    assert len(first_key) == 64
    assert first_key != second_key


@pytest.mark.parametrize("name", ["bad-name", "class", "../demo"])
def test_startproject_rejects_invalid_names(name: str, tmp_path: Path) -> None:
    destination = tmp_path / "demo"

    with pytest.raises(ValueError, match="valid Python identifier"):
        startproject(name, destination)

    assert not destination.exists()


def test_startproject_refuses_to_overwrite_nonempty_directory(tmp_path: Path) -> None:
    destination = tmp_path / "demo"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("existing data")

    with pytest.raises(FileExistsError, match="not empty"):
        startproject("demo", destination)

    assert marker.read_text() == "existing data"
    assert list(destination.iterdir()) == [marker]


def test_startproject_command_defaults_to_named_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["startproject", "demo"]) == 0

    output = capsys.readouterr().out
    assert "Created project 'demo'" in output
    assert {path.name for path in (tmp_path / "demo").iterdir()} == PROJECT_FILES


def test_generated_project_runs_with_plain_fastapi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    project = startproject("generated", tmp_path / "generated")
    monkeypatch.delenv("OBOX_DATABASE_URL", raising=False)
    spec = importlib.util.spec_from_file_location("setting", project / "setting.py")
    assert spec is not None
    assert spec.loader is not None
    setting = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "setting", setting)
    spec.loader.exec_module(setting)
    main_spec = importlib.util.spec_from_file_location("generated_main", project / "main.py")
    assert main_spec is not None
    assert main_spec.loader is not None
    module = importlib.util.module_from_spec(main_spec)
    main_spec.loader.exec_module(module)
    assert type(module.app) is FastAPI
    assert module.app.state.obox is module.obox

    async def scenario() -> None:
        async with (
            module.app.router.lifespan_context(module.app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=module.app), base_url="http://test"
            ) as client,
        ):
            response = await client.get("/")
            assert response.json() == {"message": "Welcome to generated"}
            login_redirect = await client.get("/auth/login")
            assert login_redirect.status_code == 303
            assert login_redirect.headers["location"].endswith("/auth/login")
            login_form = await client.get(login_redirect.headers["location"])
            assert login_form.status_code == 200
        assert module.obox.database.engine is None
        assert not (project / "db.sqlite3").exists()

    asyncio.run(scenario())


def test_generation_failure_preserves_existing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates = tmp_path / "templates"
    (templates / "app_template").mkdir(parents=True)
    (templates / "app_template" / "editorconfig.tmpl").write_text("root = true\n")
    monkeypatch.setattr(cli, "files", lambda _: templates)
    project = tmp_path / "existing"
    project.mkdir()
    (project / "keep.txt").write_text("keep")
    with pytest.raises(SystemExit):
        main(["startproject", str(project)])
    assert {p.name for p in project.iterdir()} == {"keep.txt"}
    assert (project / "keep.txt").read_text() == "keep"
