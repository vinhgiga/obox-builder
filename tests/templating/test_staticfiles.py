from __future__ import annotations

# Finder and collection behavior cases are adapted from Django's staticfiles_tests.
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
    url = static("shop/logo.txt")
    assert url.startswith("/static/shop/logo.txt?v=")
    assert static("/shop/logo.txt") == url
    assert static("missing.txt") == "/static/missing.txt"


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


def test_collect_static_keeps_first_source_on_duplicate(
    env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = tmp_path / "extra"
    (extra / "shop").mkdir(parents=True)
    (extra / "shop" / "logo.txt").write_text("override\n")
    root = tmp_path / "collected"
    _reload_settings(
        monkeypatch,
        OBOX_STATIC_ROOT=str(root),
        OBOX_STATICFILES_DIRS=f'["{extra.as_posix()}"]',
    )

    result = collect_static()

    assert result.copied.count("shop/logo.txt") == 1
    assert result.duplicates.count("shop/logo.txt") == 1
    assert (root / "shop" / "logo.txt").read_text() == "override\n"


def test_collect_static_preserves_unicode_and_percent_filenames(
    env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = tmp_path / "extra"
    extra.mkdir()
    names = ("⊗.txt", "%2F.txt")
    for name in names:
        (extra / name).write_text(name, encoding="utf-8")
    root = tmp_path / "collected"
    _reload_settings(
        monkeypatch,
        OBOX_STATIC_ROOT=str(root),
        OBOX_STATICFILES_DIRS=f'["{extra.as_posix()}"]',
    )

    result = collect_static()

    assert all(name in result.copied for name in names)
    assert [(root / name).read_text(encoding="utf-8") for name in names] == list(names)


def test_app_static_files_falls_back_to_later_directories(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (second / "fallback.txt").write_bytes(b"found\n")
    application = Starlette(routes=[Mount("/static", app=AppStaticFiles([first, second]))])

    response = TestClient(application).get("/static/fallback.txt")

    assert response.status_code == 200
    assert response.text == "found\n"


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
    logo = SUPPORT_DIR / "apps" / "shop" / "static" / "shop" / "logo.txt"
    assert client.get("/static/shop/logo.txt").content == logo.read_bytes()
    assert client.get("/static/project.txt").status_code == 200
    assert client.get("/static/missing.txt").status_code == 404
    assert client.get("/static/../pyproject.toml").status_code == 404


def test_app_static_files_requires_directories() -> None:
    with pytest.raises(ImproperlyConfigured, match="at least one directory"):
        AppStaticFiles([])
