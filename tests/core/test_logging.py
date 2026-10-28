from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pytest

LOG_MESSAGES = """
import json
import logging
import sys
from obox_builder.conf.logging import configure_logging

root = logging.getLogger()
root.setLevel(logging.WARNING)
handler = logging.StreamHandler()
root.addHandler(handler)
access = logging.getLogger('uvicorn.access')
access.setLevel(logging.INFO)
access.addHandler(logging.StreamHandler())
access.propagate = False
configure_logging(logging_config=json.loads(sys.argv[2]), debug=sys.argv[1] == 'True')
print(json.dumps({'root_level': root.level, 'root_handler_preserved': handler in root.handlers}))
for name in (
    'obox_builder.core', 'obox.history.audit', 'aiosqlite', 'sqlalchemy.engine', 'uvicorn.access'
):
    logger = logging.getLogger(name)
    for level in ('debug', 'info', 'warning', 'error'):
        getattr(logger, level)(name + '-' + level)
"""


def _run_logging(debug: bool, config: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    result = subprocess.run(  # noqa: S603 - fixed script and test-owned configuration
        [sys.executable, "-c", LOG_MESSAGES, str(debug), json.dumps(config)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout), result.stderr


@pytest.mark.parametrize("debug", [False, True])
def test_debug_does_not_enable_dependency_logs_or_replace_root(debug: bool) -> None:
    state, output = _run_logging(debug)
    assert state == {"root_level": 30, "root_handler_preserved": True}
    for name in ("aiosqlite", "sqlalchemy.engine"):
        assert f"{name}-debug" not in output
        assert f"{name}-info" not in output
        assert f"{name}-warning" in output
        assert f"{name}-error" in output
    assert "obox_builder.core-debug" not in output
    assert ("obox_builder.core-info" in output) is debug
    assert "obox_builder.core-warning" in output
    assert "obox_builder.core-error" in output
    assert "obox.history.audit-info" in output
    assert "obox.history.audit-debug" not in output
    assert "uvicorn.access-info" in output
    assert "uvicorn.access-debug" not in output


@pytest.mark.parametrize("debug", [False, True])
def test_logging_setting_can_explicitly_enable_dependency_debug_logs(debug: bool) -> None:
    _, output = _run_logging(
        debug,
        {
            "version": 1,
            "disable_existing_loggers": False,
            "handlers": {"console": {"class": "logging.StreamHandler"}},
            "root": {"handlers": ["console"], "level": "DEBUG"},
            "loggers": {"sqlalchemy.engine": {"level": "DEBUG"}},
        },
    )
    assert "aiosqlite-debug" in output
    assert "sqlalchemy.engine-debug" in output
