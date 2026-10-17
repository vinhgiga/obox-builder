from __future__ import annotations

from pathlib import Path

import pytest
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.staticfiles import (
    AppStaticFiles,
    app_static_dirs,
    collect_static,
    extra_static_dirs,
    find_static_directories,
    static,
)
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from tests.support.templating_settings import SUPPORT_DIR


def _reload_settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    import obox_builder
    from obox_builder.conf import settings

    for name, value in env.items():
        monkeypatch.setenv(name, value)
    settings.reset()
    obox_builder.setup()


def test_finders_put_extra_dirs_first(env: None) -> None:
    extra = extra_static_dirs()
    apps_dirs = app_static_dirs()
    assert extra == [SUPPORT_DIR / "staticfiles"]
    assert any(path.name == "static" and path.parent.name == "shop" for path in apps_dirs)
    assert find_static_directories()[: len(extra)] == extra


def test_static_url_helper(env: None) -> None:
    assert static("shop/logo.txt") == "/static/shop/logo.txt"
    assert static("/shop/logo.txt") == "/static/shop/logo.txt"


def test_collect_static_copies_files(
    env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reload_settings(monkeypatch, OBOX_STATIC_ROOT=str(tmp_path / "collected"))
    result = collect_static()
    assert "project.txt" in result.copied
    assert "shop/logo.txt" in result.copied
    assert (tmp_path / "collected" / "project.txt").read_text() == "project-asset\n"
    assert (tmp_path / "collected" / "shop" / "logo.txt").exists()


def test_collect_static_clear_removes_stale_files(
    env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "collected"
    root.mkdir()
    stale = root / "stale.txt"
    stale.write_text("old")
    _reload_settings(monkeypatch, OBOX_STATIC_ROOT=str(root))
    result = collect_static(clear=True)
    assert result.cleared
    assert not stale.exists()


def test_collect_static_dry_run_writes_nothing(
    env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "collected"
    _reload_settings(monkeypatch, OBOX_STATIC_ROOT=str(root))
    result = collect_static(dry_run=True)
    assert result.copied
    assert not root.exists()


def test_collect_static_requires_static_root(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OBOX_STATIC_ROOT", raising=False)
    with pytest.raises(ImproperlyConfigured, match="STATIC_ROOT"):
        collect_static()


def test_app_static_files_serves_and_blocks_traversal(
    env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = Starlette(
        routes=[Mount("/static", app=AppStaticFiles(find_static_directories()))]
    )
    client = TestClient(application)
    assert client.get("/static/shop/logo.txt").text == "shop-logo\n"
    assert client.get("/static/project.txt").status_code == 200
    assert client.get("/static/missing.txt").status_code == 404
    assert client.get("/static/../pyproject.toml").status_code == 404


def test_app_static_files_requires_directories() -> None:
    with pytest.raises(ImproperlyConfigured, match="at least one directory"):
        AppStaticFiles([])
