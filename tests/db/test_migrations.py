from __future__ import annotations

import sys
from pathlib import Path
from runpy import run_path
from string import Template

import pytest
import sqlalchemy as sa
from obox_builder.conf import SETTINGS_MODULE_ENV
from obox_builder.core.management.cli import create_typer_app
from obox_builder.core.management.templates import PROJECT_TEMPLATE_DIR
from obox_builder.core.signals import post_migrate
from typer.testing import CliRunner

PROJECT_SETTINGS = """\
from pydantic import SecretStr

from obox_builder.conf import BaseSettings


class Settings(BaseSettings):
    SECRET_KEY: SecretStr = SecretStr("proj-secret-key-0123456789abcdef")
    DATABASE_URL: str = "sqlite+aiosqlite:///%(db_path)s"
    INSTALLED_APPS: list[str] = ["tests.support.apps.shop"]
"""


def _render_template_file(source: Path) -> str:
    text = source.read_text()
    if source.name.endswith("-tpl"):
        return Template(text).safe_substitute()
    return text


def make_project(tmp_path: Path) -> Path:
    """Create a minimal project on disk whose settings point at a temp SQLite DB."""
    project = tmp_path / "proj"
    config_dir = project / "config"
    config_dir.mkdir(parents=True)
    (project / "__init__.py").write_text("")
    (config_dir / "__init__.py").write_text("")
    db_path = project / "db.sqlite3"
    (config_dir / "settings.py").write_text(PROJECT_SETTINGS % {"db_path": db_path.as_posix()})
    migrations = project / "migrations"
    for source in sorted((PROJECT_TEMPLATE_DIR / "migrations").rglob("*")):
        if source.is_dir():
            continue
        relative = source.relative_to(PROJECT_TEMPLATE_DIR / "migrations")
        name = str(relative)
        if name.endswith("-tpl"):
            name = name.removesuffix("-tpl")
        target = migrations / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_render_template_file(source))
    (migrations / "versions").mkdir(parents=True, exist_ok=True)
    return project


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in list(sys.modules):
        if name == "proj" or name.startswith("proj."):
            del sys.modules[name]
    root = make_project(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(SETTINGS_MODULE_ENV, "proj.config.settings")
    monkeypatch.delenv("OBOX_DATABASE_URL", raising=False)
    import obox_builder

    obox_builder.setup()
    return root


def _table_names(project: Path, database: str = "db.sqlite3") -> set[str]:
    engine = sa.create_engine(f"sqlite:///{project / database}")
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_makemigrations_then_migrate_creates_tables(project: Path) -> None:
    from obox_builder.db.migrations import current_revision, make_migration, upgrade

    assert current_revision() is None
    path = make_migration("init")
    assert path is not None
    assert path.is_file()
    assert path.parent == project / "migrations" / "versions"

    assert _table_names(project) <= {"alembic_version"}
    upgrade()
    current = current_revision()
    assert current is not None
    assert current in path.read_text()
    tables = _table_names(project)
    assert {
        "obox_category",
        "obox_product",
        "obox_order",
        "obox_order_items",
        "alembic_version",
    } <= tables


def test_second_makemigrations_detects_no_changes(project: Path) -> None:
    from obox_builder.db.migrations import make_migration, upgrade

    first = make_migration("init")
    assert first is not None
    upgrade()
    assert make_migration("again") is None
    versions = list((project / "migrations" / "versions").glob("*.py"))
    assert len(versions) == 1


def test_makemigrations_empty_creates_revision_without_changes(project: Path) -> None:
    from obox_builder.db.migrations import make_migration

    path = make_migration("empty", empty=True)
    assert path is not None
    assert path.is_file()


def test_migration_history_marks_applied_revisions(project: Path) -> None:
    from obox_builder.db.migrations import make_migration, migration_history, upgrade

    path = make_migration("init")
    assert path is not None
    revision = run_path(str(path))["revision"]
    assert migration_history() == [(revision, False)]
    upgrade()
    assert migration_history() == [(revision, True)]


def test_makemigrations_ignores_tables_of_uninstalled_apps(project: Path) -> None:
    import sqlalchemy as sa
    from obox_builder.db.base import ModelBase
    from obox_builder.db.migrations import make_migration

    stray = sa.Table(
        "stray_table", ModelBase.metadata, sa.Column("id", sa.Integer, primary_key=True)
    )
    try:
        path = make_migration("init")
        assert path is not None
        assert "stray_table" not in path.read_text()
    finally:
        ModelBase.metadata.remove(stray)


def test_shared_migration_graph_filters_operations_per_database_alias(
    project: Path,
) -> None:
    settings_file = project / "config" / "settings.py"
    analytics_url = f"sqlite+aiosqlite:///{project / 'analytics.sqlite3'}"
    extra_settings = (
        f"\n    DATABASES: dict[str, str] = {{'analytics': {analytics_url!r}}}"
        "\n    DATABASE_ROUTERS: list[str] = "
        "['tests.support.database_routers.ShopRouter']\n"
    )
    settings_file.write_text(settings_file.read_text() + extra_settings)
    from obox_builder.conf import settings
    import obox_builder

    settings.reset()
    obox_builder._reset()
    sys.modules.pop("proj.config.settings", None)

    from obox_builder.db.migrations import (
        current_revision,
        make_migration,
        migration_history,
    )

    runner = CliRunner()
    result = runner.invoke(
        create_typer_app(), ["makemigrations", "--database", "analytics", "-m", "analytics init"]
    )
    assert result.exit_code == 0, result.output
    path = next((project / "migrations" / "versions").glob("*.py"))
    source = path.read_text()
    assert path.parent == project / "migrations" / "versions"
    assert "upgrade_db_" in source
    assert "obox_product" in source
    revision = run_path(str(path))["revision"]
    assert migration_history("default") == [(revision, False)]
    assert migration_history("analytics") == [(revision, False)]

    result = runner.invoke(create_typer_app(), ["migrate", "--database", "analytics"])
    assert result.exit_code == 0, result.output
    assert current_revision("analytics") is not None
    assert current_revision("default") is None
    assert migration_history("analytics")[-1][1] is True
    assert "obox_product" in _table_names(project, "analytics.sqlite3")

    result = runner.invoke(create_typer_app(), ["showmigrations", "--database", "analytics"])
    assert result.exit_code == 0, result.output
    assert "[X]" in result.output

    result = runner.invoke(create_typer_app(), ["migrate"])
    assert result.exit_code == 0, result.output
    assert current_revision("default") == current_revision("analytics")
    assert "obox_product" not in _table_names(project)
    assert make_migration("again") is None


def test_migration_refuses_an_alias_missing_from_existing_revisions(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obox_builder.conf import settings
    from obox_builder.db.migrations import make_migration, upgrade

    assert make_migration("default init") is not None
    upgrade(database="default")
    analytics_url = f"sqlite+aiosqlite:///{project / 'analytics.sqlite3'}"
    monkeypatch.setitem(settings.DATABASES, "analytics", analytics_url)

    with pytest.raises(RuntimeError, match="has no operations for database 'db_analytics_"):
        upgrade(database="analytics")


def test_cli_makemigrations_migrate_showmigrations(project: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(create_typer_app(), ["makemigrations", "-m", "init"])
    assert result.exit_code == 0, result.output
    assert "Created migration" in result.output

    recorded: list[str] = []
    post_migrate.connect(
        lambda sender, **kwargs: recorded.append("post_migrate"), dispatch_uid="test"
    )

    result = runner.invoke(create_typer_app(), ["migrate"])
    assert result.exit_code == 0, result.output
    assert recorded == ["post_migrate"]

    result = runner.invoke(create_typer_app(), ["showmigrations"])
    assert result.exit_code == 0, result.output
    assert "[X]" in result.output

    result = runner.invoke(create_typer_app(), ["makemigrations", "-m", "again"])
    assert result.exit_code == 0, result.output
    assert "No changes detected." in result.output


def test_cli_migrate_sql_prints_statements(project: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(create_typer_app(), ["makemigrations", "-m", "init"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(create_typer_app(), ["migrate", "--sql"])
    assert result.exit_code == 0, result.output
    assert "CREATE TABLE obox_product" in result.output
    assert _table_names(project) <= {"alembic_version"}
