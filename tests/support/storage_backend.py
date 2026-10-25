from __future__ import annotations

from io import BytesIO
from typing import BinaryIO


class MemoryStorage:
    files: dict[str, bytes] = {}

    def save(self, name: str, content: bytes) -> str:
        if name in self.files:
            raise FileExistsError(name)
        self.files[name] = content
        return name

    def open(self, name: str, mode: str = "rb") -> BinaryIO:
        if mode != "rb":
            raise ValueError(mode)
        return BytesIO(self.files[name])

    def exists(self, name: str) -> bool:
        return name in self.files

    def delete(self, name: str) -> None:
        self.files.pop(name, None)
