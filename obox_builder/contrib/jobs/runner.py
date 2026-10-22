"""Private child-process entry point. Only JSON crosses the process boundary."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import sys
import threading
import traceback
from importlib import import_module
from pathlib import Path
from typing import Any

import obox_builder
from obox_builder.contrib.jobs.registry import TaskRequest, _request, encode_json, get_task
from obox_builder.db import dispose
from obox_builder.utils.module_loading import import_string


def error_trace(exc: BaseException) -> str:
    """Retain frame locations and error types, excluding source, values and locals."""
    frames = traceback.extract_tb(exc.__traceback__)
    lines = [f"  {frame.filename}:{frame.lineno} in {frame.name}" for frame in frames]
    return "\n".join([*lines, type(exc).__name__])[-16_384:]


async def execute(data: dict[str, Any]) -> dict[str, Any]:
    retryable = False
    try:
        config_class = import_string(data["settings_class"])
        obox_builder.configure(config_class.model_validate(data["settings"]))
        obox_builder.setup()
        import_module(data["module"])
        definition = get_task(data["name"])
        token = _request.set(
            TaskRequest(data["task_id"], data["name"], data["retries"], data["worker"])
        )
        try:
            try:
                value = definition(*data["args"], **data["kwargs"])
                if inspect.isawaitable(value):
                    value = await value
                result = json.loads(encode_json(value))
            except Exception as exc:
                retryable = isinstance(exc, definition.autoretry_for)
                raise
        finally:
            _request.reset(token)
        return {"status": "SUCCESS", "result": result}
    except Exception as exc:
        return {"status": "FAILURE", "traceback": error_trace(exc), "retryable": retryable}
    finally:
        await dispose()


def main() -> None:
    input_path, output_path = map(Path, sys.argv[1:])

    # The parent keeps this pipe open. EOF also arrives after a hard parent
    # crash, so orphaned CPU work cannot survive indefinitely on Windows.
    def watch_parent() -> None:
        # Buffered stdin locks can deadlock interpreter shutdown in a daemon
        # thread. Reading the raw descriptor avoids that teardown race.
        os.read(sys.stdin.fileno(), 1)
        shutil.rmtree(output_path.parent, ignore_errors=True)
        os._exit(70)

    threading.Thread(target=watch_parent, daemon=True).start()
    try:
        data = json.loads(input_path.read_text(encoding="utf-8"))
    finally:
        input_path.unlink(missing_ok=True)
    result = asyncio.run(execute(data))
    output_path.write_text(
        json.dumps(result, allow_nan=False, ensure_ascii=False), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
