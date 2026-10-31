"""Private child-process entry point. Only JSON crosses the process boundary."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import threading
import traceback
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any

from obox_builder.contrib.jobs.registry import TaskRequest, _request, encode_json, get_task


def error_trace(exc: BaseException) -> str:
    """Retain frame locations and error types, excluding source, values and locals."""
    frames = traceback.extract_tb(exc.__traceback__)
    lines = [f"  {frame.filename}:{frame.lineno} in {frame.name}" for frame in frames]
    return "\n".join([*lines, type(exc).__name__])[-16_384:]


async def execute(data: dict[str, Any]) -> dict[str, Any]:
    retryable = False
    try:
        if module := os.environ.get("OBOX_SETTINGS_MODULE"):
            import_module(module)
        import_module(data["module"])
        definition = get_task(data["name"])
        token = _request.set(
            TaskRequest(
                data["task_id"],
                data["name"],
                data["retries"],
                data["worker"],
                datetime.fromisoformat(data["expires"]) if data.get("expires") else None,
                data["priority"],
            )
        )
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


def main() -> None:
    input_path, output_path = map(Path, sys.argv[1:])

    # The parent keeps this pipe open. EOF also arrives after a hard parent
    # crash, so orphaned CPU work cannot survive indefinitely on Windows.
    def watch_parent() -> None:
        # Buffered stdin locks can deadlock interpreter shutdown in a daemon
        # thread. Reading the raw descriptor avoids that teardown race.
        os.read(sys.stdin.fileno(), 1)
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
    # Each process runs one task; leftover library threads must not delay its result.
    os._exit(0)


if __name__ == "__main__":
    main()
