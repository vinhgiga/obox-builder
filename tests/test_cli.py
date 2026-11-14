from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from obox_builder import AppConfig, ModelBuilder, Principal, cli
from obox_builder.cli import main, startproject
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.orm.exc import StaleDataError

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


@pytest.mark.parametrize("frontend", [False, True])
@pytest.mark.parametrize("root_path", ["", "/prefix"])
def test_generated_project_runs_with_plain_fastapi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frontend: bool, root_path: str
) -> None:
    import sys

    project = startproject("generated", tmp_path / "generated")
    if frontend:
        output = project / "frontend" / "dist"
        output.mkdir(parents=True)
        (output / "index.html").write_text("<html>Frontend application</html>")
        (output / "app.js").write_text("export const ready = true;")
    monkeypatch.delenv("OBOX_DATABASE_URL", raising=False)
    spec = importlib.util.spec_from_file_location("setting", project / "setting.py")
    assert spec is not None
    assert spec.loader is not None
    setting = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "setting", setting)
    spec.loader.exec_module(setting)

    class Base(DeclarativeBase):
        pass

    class Record(Base):
        __tablename__ = "scaffold_records"
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str]

    class Records(ModelBuilder[Record]):
        fields = ("id", "name")

    class Sample(AppConfig):
        name = "obox_builder"
        label = "sample"
        builders = (Records,)

    vars(setting)["INSTALLED_APPS"] = (Sample,)
    vars(setting)["PRINCIPAL_DEPENDENCY"] = lambda: Principal(
        id=1, is_authenticated=True, is_superuser=True
    )
    main_spec = importlib.util.spec_from_file_location("generated_main", project / "main.py")
    assert main_spec is not None
    assert main_spec.loader is not None
    module = importlib.util.module_from_spec(main_spec)
    main_spec.loader.exec_module(module)
    assert type(module.app) is FastAPI
    assert module.app.state.obox is module.obox
    assert module.obox.settings.ui_prefix == "/admin"
    assert module.obox.settings.api_default_limit == 100
    assert module.obox.settings.api_max_limit == 1000
    assert module.obox.settings.api_max_batch == 100

    async def scenario() -> None:
        async with (
            module.app.router.lifespan_context(module.app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=module.app, root_path=root_path),
                base_url="http://test",
            ) as client,
        ):
            response = await client.get(root_path + "/")
            if frontend:
                assert response.status_code == 200
                assert "Frontend application" in response.text
                navigation = await client.get(
                    root_path + "/dashboard/settings", headers={"Accept": "text/html"}
                )
                assert navigation.status_code == 200
                assert navigation.text == response.text
                assert (await client.get(root_path + "/app.js")).status_code == 200
                assert (await client.get(root_path + "/missing.js")).status_code == 404
            else:
                assert response.status_code == 307
                assert response.headers["location"] == f"http://test{root_path}/admin"
            catalog = await client.get(root_path + "/admin")
            assert catalog.status_code == 200
            assert "Applications" in catalog.text
            form = await client.get(root_path + "/admin/sample/records/new")
            assert form.status_code == 200
            assert 'name="name"' in form.text
            metadata = await client.get(root_path + "/api/sample/records:describe")
            assert metadata.status_code == 200
            assert metadata.json()["slug"] == "sample/records"
            csrf = await client.get(root_path + "/api/auth/csrf")
            assert csrf.status_code == 200
            assert "csrfToken" in csrf.json()
            missing = await client.get(
                root_path + "/api/missing", headers={"Accept": "application/json"}
            )
            assert missing.status_code == 404
            assert missing.headers["content-type"] == "application/problem+json"
        assert module.obox.database.engine is None
        assert not (project / "db.sqlite3").exists()

    asyncio.run(scenario())


def test_generated_base_versions_each_model_and_rejects_stale_writes(tmp_path: Path) -> None:
    project = startproject("generated", tmp_path / "generated")
    spec = importlib.util.spec_from_file_location("generated_models", project / "models.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    first: Any = type(
        "First",
        (module.Base,),
        {
            "__tablename__": "first",
            "id": mapped_column(sa.Integer, primary_key=True),
            "name": mapped_column(sa.String, nullable=False),
        },
    )
    second: Any = type(
        "Second",
        (module.Base,),
        {
            "__tablename__": "second",
            "id": mapped_column(sa.Integer, primary_key=True),
        },
    )
    assert sa.inspect(first).version_id_col is first.__table__.c.version
    assert sa.inspect(second).version_id_col is second.__table__.c.version

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(module.Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as session:
                session.add(first(name="initial"))
                await session.commit()
            async with sessions() as left, sessions() as right:
                current = (await left.scalars(sa.select(first))).one()
                stale = (await right.scalars(sa.select(first))).one()
                current.name = "new"
                await left.commit()
                assert current.version == 2
                stale.name = "old"
                with pytest.raises(StaleDataError):
                    await right.commit()
        finally:
            await engine.dispose()

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
