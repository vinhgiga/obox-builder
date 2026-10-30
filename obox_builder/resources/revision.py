"""Stable record revisions shared by non-HTTP callers and HTTP validators."""

import hashlib
import json
from typing import Any


def record_revision(fields: dict[str, Any], version: Any = None) -> str:
    return hashlib.sha256(
        json.dumps(
            [version, fields], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
