"""Private file references stored as relative paths in text columns."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from obox_builder.fields.base import Cell, FieldSpec, FieldType

DEFAULT_ACCEPT = (
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
    "text/plain",
)


class File(FieldType):
    """A single private upload. Configure with ``fields_config`` on a string column."""

    name = "file"
    widget = "file"
    sortable = False

    def annotation(self, spec: FieldSpec) -> Any:
        return str | None if not spec.required else str

    @property
    def accept(self) -> tuple[str, ...]:
        configured = self.options.get("accept", DEFAULT_ACCEPT)
        return tuple(configured)

    @property
    def max_bytes(self) -> int:
        return min(max(int(self.options.get("max_bytes", 10 * 1024 * 1024)), 1), 50 * 1024 * 1024)

    def to_input(self, value: Any, spec: FieldSpec) -> str:
        return "" if value is None else str(value)

    def cell(self, value: Any, spec: FieldSpec) -> Cell:
        text = PurePosixPath(str(value)).name if value else ""
        return Cell(text=text, value=value, kind="file")

    def diff_repr(self, value: Any) -> Any:
        return value
