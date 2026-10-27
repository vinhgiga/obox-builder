"""Strong validators over canonical record state, including relation values."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from obox_builder.conf import settings
from obox_builder.contrib.admin.api.problems import Problem


def etag(fields: dict[str, Any], version: Any = None) -> str:
    digest = hashlib.sha256(
        json.dumps(
            [version, fields], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    return f'"{digest}"'


def check_if_match(current: str, provided: str | None) -> None:
    if provided is None:
        if settings.ADMIN_API_REQUIRE_IF_MATCH:
            raise Problem(428, "precondition_required", "If-Match is required.")
        return
    if provided.strip() == "*":
        return
    tags = re.fullmatch(
        r'\s*(?:W/)?"[^"\x00-\x20]*"(?:\s*,\s*(?:W/)?"[^"\x00-\x20]*")*\s*', provided
    )
    if not tags or current not in [
        tag.strip() for tag in provided.split(",") if not tag.strip().startswith("W/")
    ]:
        raise Problem(412, "precondition_failed", "The record has changed.")
