"""Capture plans: which fields of a tracked model are recorded and how they diff.

A tracker resolves once per model (``ready()``-style) from the same
``introspect_model`` data the admin uses, so field labels, ``secret`` markers
and ``FieldType.diff_repr`` stay consistent with the UI. The diff itself only
reads SQLAlchemy attribute history; it never triggers lazy loads (all
relationships default to ``lazy="raise"``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm.base import NO_VALUE

from obox_builder.apps import apps
from obox_builder.contrib.history.options import HistoryOptions, parse_history
from obox_builder.db.mixins import TimestampMixin
from obox_builder.fields import FieldSpec, introspect_model
from obox_builder.utils.strings import camel_to_snake

__all__ = [
    "PendingChange",
    "TrackedField",
    "Tracker",
    "declared_models",
    "get_tracker",
    "is_tracked",
    "track",
]

TIMESTAMP_FIELDS = frozenset({"created_at", "updated_at"})

_UNKNOWN = object()


@dataclass(frozen=True)
class TrackedField:
    """One attribute recorded by a tracker."""

    name: str
    label: str
    spec: FieldSpec
    #: ``True`` for secrets and masked fields: only the ``redacted`` marker is
    #: recorded, never the values.
    secret: bool = False


@dataclass
class PendingChange:
    """A diff computed in ``before_flush``, awaiting the INSERT in ``after_flush``."""

    tracker: Tracker
    op: str
    obj: Any
    fields: dict[str, list[Any]] = field(default_factory=dict)
    redacted: list[str] = field(default_factory=list)
    unknown_old: list[str] = field(default_factory=list)

    @property
    def has_effect(self) -> bool:
        return bool(self.fields or self.redacted or self.unknown_old)


class Tracker:
    """Resolved capture plan for one model."""

    __slots__ = ("fields", "model", "model_label", "options", "pk_key", "relations")

    def __init__(self, model: type[Any], options: HistoryOptions) -> None:
        self.model = model
        self.options = options
        mapper = sa.inspect(model)
        self.model_label = f"{model._model_label}.{camel_to_snake(model.__name__)}"
        self.pk_key = mapper.primary_key[0].key
        self.fields = _select_fields(model, options)
        self.relations = options.relations

    def pk(self, obj: Any) -> Any:
        return getattr(obj, self.pk_key, None)

    def object_repr(self, obj: Any) -> str:
        """Snapshot of ``str(obj)`` with a ``#pk`` fallback (survives deletion)."""
        try:
            text = str(obj)
        except Exception:  # noqa: BLE001 - __str__ may touch unloaded relations
            text = ""
        if not text or text.startswith("<"):
            return f"#{self.pk(obj)}"
        return text[:255]

    def diff(self, obj: Any, op: str) -> PendingChange | None:
        """Compute the field-level change for ``obj`` without emitting IO."""
        state = sa.inspect(obj)
        change = PendingChange(tracker=self, op=op, obj=obj)
        for tf in self.fields:
            try:
                attr = state.attrs[tf.name]
            except KeyError:  # pragma: no cover - defensive
                continue
            history = attr.history
            if tf.secret:
                if op != "delete" and history.has_changes():
                    change.redacted.append(tf.name)
                continue
            if op == "create":
                # Column defaults only exist once the INSERT ran, so the
                # snapshot is filled from ``fill_create`` in ``after_flush``.
                continue
            if op == "update":
                if not history.has_changes():
                    continue
                old: Any = history.deleted[0] if history.deleted else _UNKNOWN
                new: Any = history.added[-1] if history.added else attr.loaded_value
                if old is _UNKNOWN or new is NO_VALUE or new is _UNKNOWN:
                    change.unknown_old.append(tf.name)
                    continue
                old_repr = tf.spec.type.diff_repr(old)
                new_repr = tf.spec.type.diff_repr(new)
                if old_repr == new_repr:
                    continue
                change.fields[tf.name] = [old_repr, new_repr]
            else:  # delete
                value = attr.loaded_value
                if value is NO_VALUE:
                    change.unknown_old.append(tf.name)
                    continue
                value_repr = tf.spec.type.diff_repr(value)
                if value_repr is None:
                    continue
                change.fields[tf.name] = [value_repr, None]
        # CREATE snapshots are completed in ``after_flush`` (column defaults),
        # so an empty create still has to reach the write stage.
        if op == "create" or change.has_effect:
            return change
        return None

    def fill_create(self, change: PendingChange) -> None:
        """Fill a CREATE snapshot from current attribute values (``after_flush``)."""
        state = sa.inspect(change.obj)
        for tf in self.fields:
            if tf.secret:
                continue
            try:
                attr = state.attrs[tf.name]
            except KeyError:  # pragma: no cover - defensive
                continue
            value = attr.loaded_value
            if value is NO_VALUE or value is None:
                continue
            value_repr = tf.spec.type.diff_repr(value)
            if value_repr is None:
                continue
            change.fields[tf.name] = [None, value_repr]


def _select_fields(model: type[Any], options: HistoryOptions) -> list[TrackedField]:
    is_timestamped = issubclass(model, TimestampMixin)
    fields: list[TrackedField] = []
    for spec in introspect_model(model):
        if spec.primary_key:
            continue
        if is_timestamped and spec.name in TIMESTAMP_FIELDS:
            continue
        if spec.name in options.exclude:
            continue
        if options.fields is not None and spec.name not in options.fields:
            continue
        if not spec.history and not spec.secret:
            continue
        # Secrets and masked fields are recorded as a ``redacted`` marker only
        # (partial masking arrives in a later phase).
        secret = spec.secret or spec.name in options.mask
        fields.append(TrackedField(name=spec.name, label=spec.label, spec=spec, secret=secret))
    return fields


# -- registry ---------------------------------------------------------------

_trackers: dict[type[Any], Tracker | None] = {}
_explicit: dict[type[Any], HistoryOptions] = {}
_built = False


def track(
    model: type[Any],
    *,
    fields: list[str] | tuple[str, ...] | None = None,
    exclude: list[str] | tuple[str, ...] = (),
    relations: list[str] | tuple[str, ...] = (),
    mask: list[str] | tuple[str, ...] = (),
    retention_days: int | None = None,
) -> None:
    """Explicitly register a model for tracking (for third-party models).

    Explicit options take precedence over a ``Model.__history__`` declaration.
    """
    _explicit[model] = HistoryOptions(
        enabled=True,
        fields=tuple(fields) if fields is not None else None,
        exclude=frozenset(exclude),
        relations=tuple(relations),
        mask=frozenset(mask),
        retention_days=retention_days,
    )
    _trackers.pop(model, None)


def _options_for(model: type[Any]) -> HistoryOptions:
    declared = _explicit.get(model)
    if declared is not None:
        return declared
    return parse_history(getattr(model, "__history__", None))


def _build_for(model: type[Any]) -> Tracker | None:
    options = _options_for(model)
    if not options.enabled:
        return None
    if len(sa.inspect(model).primary_key) != 1:
        return None
    tracker = Tracker(model, options)
    return tracker if tracker.fields else None


def _ensure_built() -> None:
    global _built
    if _built or not apps.models_ready:
        return
    _built = True
    for model in apps.get_models():
        _trackers.setdefault(model, _build_for(model))


def get_tracker(model: type[Any]) -> Tracker | None:
    """The resolved tracker for ``model``, or ``None`` when not tracked."""
    _ensure_built()
    if model not in _trackers:
        _trackers[model] = _build_for(model)
    return _trackers[model]


def is_tracked(model: type[Any]) -> bool:
    return get_tracker(model) is not None


def declared_models() -> list[tuple[type[Any], HistoryOptions]]:
    """Every model that opts into tracking (for system checks)."""
    if not apps.models_ready:
        return []
    result: list[tuple[type[Any], HistoryOptions]] = []
    for model in apps.get_models():
        options = _options_for(model)
        if options.enabled:
            result.append((model, options))
    return result


def _reset() -> None:
    """Test hook: drop cached trackers and explicit registrations."""
    global _built
    _trackers.clear()
    _explicit.clear()
    _built = False
