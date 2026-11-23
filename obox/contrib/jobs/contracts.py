"""Stable task names and bounded JSON payloads."""

from __future__ import annotations

import json
import re
from typing import Any

NAME_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_.:-]{0,154}$")


def encode_json(value: Any) -> str:
    encoded = json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 1_048_576:
        raise ValueError("Task data exceeds 1 MiB.")
    return encoded
