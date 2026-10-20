"""Input validation for comment writes (trim, normalise, bound)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from obox_builder.contrib.comments.exceptions import CommentValidationError

MAX_IDEMPOTENCY_KEY = 64


@dataclass(frozen=True)
class CommentInput:
    body: str
    parent_id: int | None = None
    idempotency_key: str | None = None


def parse_comment_input(data: Mapping[str, Any], *, max_length: int) -> CommentInput:
    body = normalize_body(data.get("body"), max_length=max_length)
    parent_id = parse_parent_id(data.get("parent_id"))
    key = parse_idempotency_key(data.get("idempotency_key"))
    return CommentInput(body=body, parent_id=parent_id, idempotency_key=key)


def normalize_body(raw: Any, *, max_length: int) -> str:
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise CommentValidationError("Comment body must be text.", field="body")
    body = raw.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in body:
        raise CommentValidationError("Comment body contains an invalid character.", field="body")
    body = body.strip()
    if not body:
        raise CommentValidationError("Write something before posting.", field="body")
    if len(body) > max_length:
        raise CommentValidationError(
            f"Comment is too long ({len(body)} characters; the limit is {max_length}).",
            field="body",
        )
    return body


def parse_parent_id(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise CommentValidationError("Invalid parent comment.", field="parent_id")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise CommentValidationError("Invalid parent comment.", field="parent_id") from None
    if value <= 0:
        raise CommentValidationError("Invalid parent comment.", field="parent_id")
    return value


def parse_idempotency_key(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise CommentValidationError("Invalid idempotency key.", field="idempotency_key")
    value = raw.strip()
    if not value:
        return None
    if len(value) > MAX_IDEMPOTENCY_KEY:
        raise CommentValidationError(
            "Invalid idempotency key (maximum 64 characters).", field="idempotency_key"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise CommentValidationError("Invalid idempotency key.", field="idempotency_key")
    return value


__all__ = [
    "CommentInput",
    "normalize_body",
    "parse_comment_input",
    "parse_idempotency_key",
    "parse_parent_id",
]
