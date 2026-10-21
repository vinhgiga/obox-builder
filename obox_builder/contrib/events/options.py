"""Data capture reuses history's diff engine and privacy declarations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from obox_builder.contrib.history.options import HistoryOptions, parse_history
from obox_builder.contrib.history.tracker import Tracker
from obox_builder.core.exceptions import ImproperlyConfigured


@dataclass(frozen=True)
class EventOptions:
    history: HistoryOptions
    previous: bool = False


def parse_events(model: type[Any]) -> EventOptions:
    raw = getattr(model, "__events__", None)
    if raw is None or raw is False:
        return EventOptions(HistoryOptions())
    if raw is True:
        data: dict[str, Any] = {}
    elif isinstance(raw, Mapping):
        data = dict(raw)
    else:
        raise ImproperlyConfigured("__events__ must be True, False, or a mapping.")
    unknown = set(data) - {"fields", "exclude", "mask", "relations", "previous"}
    if unknown:
        raise ImproperlyConfigured(f"__events__ has unknown keys: {sorted(unknown)}.")
    previous = data.pop("previous", False)
    if not isinstance(previous, bool):
        raise ImproperlyConfigured("__events__.previous must be a boolean.")
    # Events may be stricter than history but must never undo its privacy policy.
    history = parse_history(getattr(model, "__history__", None))
    for name in ("exclude", "mask"):
        value = data.get(name, ())
        if isinstance(value, str) or not isinstance(value, (tuple, list, set, frozenset)):
            raise ImproperlyConfigured(f"__events__.{name} must be a sequence of field names.")
        data[name] = set(value) | set(getattr(history, name))
    for name in ("fields", "relations"):
        value = data.get(name)
        if value is not None and (
            isinstance(value, str) or not isinstance(value, (tuple, list, set, frozenset))
        ):
            raise ImproperlyConfigured(f"__events__.{name} must be a sequence of field names.")
    return EventOptions(parse_history(data), previous)


_trackers: dict[type[Any], tuple[Tracker, EventOptions] | None] = {}


def get_tracker(model: type[Any]) -> tuple[Tracker, EventOptions] | None:
    if model not in _trackers:
        options = parse_events(model)
        _trackers[model] = (
            (Tracker(model, options.history), options)
            if options.history.enabled and len(sa.inspect(model).primary_key) == 1
            else None
        )
    return _trackers[model]
