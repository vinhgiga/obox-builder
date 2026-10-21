"""Low-level event port: transaction-local drafts and code subscriptions.

No database or contrib imports. Installing ``contrib.events`` activates the
adapter; producers can safely emit when that optional app is absent.
"""

from __future__ import annotations

import inspect
import json
import math
import re
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from obox_builder.apps import apps
from obox_builder.conf import settings
from obox_builder.core.exceptions import ImproperlyConfigured

DRAFTS_KEY = "obox.events.drafts"
NAME_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_.:-]{0,149}$")


@dataclass(frozen=True)
class Lineage:
    event_id: uuid.UUID
    subscription: str
    hop: int


lineage: ContextVar[Lineage | None] = ContextVar("obox.events.lineage", default=None)


@dataclass(frozen=True)
class EventDraft:
    id: uuid.UUID
    type: str
    payload: dict[str, Any]
    subject: Any
    dedup_key: str | None
    schema_version: int
    transaction: Any
    parent: Lineage | None


@dataclass(frozen=True)
class Subscription:
    name: str
    pattern: str
    handler: Callable[..., Any]
    queue: str
    transactional: bool
    timeout: float
    max_attempts: int
    watch_fields: frozenset[str]
    origins: frozenset[str]
    allow_self: bool

    def matches(self, row: dict[str, Any]) -> bool:
        return (
            fnmatchcase(row["type"], self.pattern)
            and (
                not self.watch_fields or bool(self.watch_fields.intersection(row["changed_fields"]))
            )
            and (not self.origins or row["source"] in self.origins)
            and (self.allow_self or row["producer"] != self.name)
        )


_subscriptions: dict[str, Subscription] = {}


def enabled() -> bool:
    return bool(
        apps.models_ready
        and settings.EVENTS_ENABLED
        and apps.is_installed("obox_builder.contrib.events")
    )


def encode_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Snapshot JSON data now, rejecting oversized/non-JSON values atomically."""
    if not isinstance(payload, dict):
        raise ValueError("Event payload must be a JSON object.")
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > settings.EVENTS_MAX_PAYLOAD_BYTES:
        raise ValueError("Event payload exceeds EVENTS_MAX_PAYLOAD_BYTES.")
    return dict(json.loads(encoded))


def emit(
    session: Any,
    type: str,
    payload: dict[str, Any] | None = None,
    *,
    subject: Any = None,
    dedup_key: str | None = None,
    schema_version: int = 1,
) -> uuid.UUID | None:
    """Stage an event for the caller's transaction; never commit or perform I/O.

    Accepts either AsyncSession or its sync session. The returned UUID is a
    proposed id: a duplicate ``dedup_key`` keeps the original event instead.
    """
    if not enabled():
        return None
    if not NAME_RE.fullmatch(type):
        raise ValueError("Event type must be 1–150 letters, digits, or _ . : -.")
    if schema_version < 1:
        raise ValueError("schema_version must be positive.")
    if dedup_key is not None and (not dedup_key or len(dedup_key) > 255):
        raise ValueError("dedup_key must contain 1–255 characters.")
    parent = lineage.get()
    if parent is not None and parent.hop >= settings.EVENTS_MAX_HOPS:
        raise ValueError("Event chain exceeds EVENTS_MAX_HOPS.")
    data = encode_payload({} if payload is None else payload)
    sync = getattr(session, "sync_session", session)
    if not sync.in_transaction():
        sync.begin()
    transaction = sync.get_nested_transaction() or sync.get_transaction()
    event_id = uuid.uuid4()
    sync.info.setdefault(DRAFTS_KEY, []).append(
        EventDraft(event_id, type, data, subject, dedup_key, schema_version, transaction, parent)
    )
    return event_id


def on(
    pattern: str,
    *,
    name: str | None = None,
    queue: str = "default",
    transactional: bool = True,
    timeout: float = 30.0,
    max_attempts: int = 8,
    watch_fields: tuple[str, ...] = (),
    origins: tuple[str, ...] = (),
    allow_self: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register an async ``handler(event, context)`` with a stable unique name."""
    if not pattern or len(pattern) > 150 or not NAME_RE.fullmatch(queue):
        raise ImproperlyConfigured("Invalid event pattern or queue name.")
    if not math.isfinite(timeout) or timeout <= 0 or max_attempts < 1:
        raise ImproperlyConfigured("Handler timeout and max_attempts must be positive.")

    def register(handler: Callable[..., Any]) -> Callable[..., Any]:
        key = name or f"{handler.__module__}.{handler.__qualname__}"
        if len(key) > 255 or not inspect.iscoroutinefunction(handler):
            raise ImproperlyConfigured("Event handlers must be async with names up to 255 chars.")
        existing = _subscriptions.get(key)
        if existing is not None and existing.handler is not handler:
            raise ImproperlyConfigured(f"Duplicate event subscription {key!r}.")
        _subscriptions[key] = Subscription(
            key,
            pattern,
            handler,
            queue,
            transactional,
            timeout,
            max_attempts,
            frozenset(watch_fields),
            frozenset(origins),
            allow_self,
        )
        return handler

    return register


def subscriptions() -> tuple[Subscription, ...]:
    return tuple(_subscriptions.values())
