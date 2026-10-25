"""System checks for ``__history__`` declarations (plan §9)."""

from __future__ import annotations

from collections.abc import Callable

import pytest


def history_check_ids() -> set[str]:
    from obox_builder.core.checks import run_checks

    return {message.id for message in run_checks(tags=("history",)) if message.id}


def test_clean_declarations_produce_no_messages(history_env: None) -> None:
    assert history_check_ids() == set()


def test_bad_declarations_report_e201_e202_e203(monkeypatch: pytest.MonkeyPatch) -> None:
    import obox_builder
    from obox_builder.conf import SETTINGS_MODULE_ENV

    monkeypatch.setenv(SETTINGS_MODULE_ENV, "tests.support.history_check_settings")
    obox_builder._reset()
    obox_builder.setup()

    from obox_builder.core.checks import run_checks

    messages = [m for m in run_checks(tags=("history",)) if m.id]
    ids = [m.id for m in messages]
    assert "obox.history.E201" in ids  # composite primary key
    assert ids.count("obox.history.E202") == 2  # "made_up" + single-valued "parent"
    assert ids.count("obox.history.E203") == 3  # fields/exclude/mask typos
    assert ids.count("obox.history.W201") == 2  # Broken + Pair lack VersionedMixin
    assert "obox.history.W202" not in ids


def test_failure_mode_open_warns_w202(
    history_env: None, override_settings: Callable[..., None]
) -> None:
    override_settings(HISTORY_FAILURE_MODE="open")
    assert "obox.history.W202" in history_check_ids()


def test_database_check_rejects_history_models_split_from_history_store(
    history_env: None,
) -> None:
    from obox_builder.conf import settings
    from obox_builder.contrib.history.checks import check_history_database

    settings.DATABASES["analytics"] = "sqlite+aiosqlite:///analytics.sqlite3"
    settings.DATABASE_ROUTERS[:] = ["tests.support.database_routers.WarehouseRouter"]
    messages = check_history_database()
    assert any(message.id == "obox.E006" for message in messages)
