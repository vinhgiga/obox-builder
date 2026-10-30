from __future__ import annotations

import pytest
from obox_builder.cli import main, startproject


def test_startproject_creates_runnable_skeleton(tmp_path) -> None:
    project = startproject("demo", tmp_path / "demo")

    assert project == tmp_path / "demo"
    assert {path.name for path in project.iterdir()} == {
        ".editorconfig",
        "justfile",
        "main.py",
        "setting.py",
    }
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


def test_startproject_generates_a_unique_secret_per_project(tmp_path) -> None:
    first = startproject("first", tmp_path / "first")
    second = startproject("second", tmp_path / "second")

    def configured_secret(project) -> str:
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
def test_startproject_rejects_invalid_names(name: str, tmp_path) -> None:
    destination = tmp_path / "demo"

    with pytest.raises(ValueError, match="valid Python identifier"):
        startproject(name, destination)

    assert not destination.exists()


def test_startproject_refuses_to_overwrite_nonempty_directory(tmp_path) -> None:
    destination = tmp_path / "demo"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("existing data")

    with pytest.raises(FileExistsError, match="not empty"):
        startproject("demo", destination)

    assert marker.read_text() == "existing data"
    assert list(destination.iterdir()) == [marker]


def test_startproject_command_defaults_to_named_directory(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["startproject", "demo"]) == 0

    output = capsys.readouterr().out
    assert "Created project 'demo'" in output
    assert {path.name for path in (tmp_path / "demo").iterdir()} == {
        ".editorconfig",
        "justfile",
        "main.py",
        "setting.py",
    }
