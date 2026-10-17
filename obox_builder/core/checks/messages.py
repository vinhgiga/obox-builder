from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class CheckLevel(IntEnum):
    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40


@dataclass(frozen=True)
class CheckMessage:
    level: CheckLevel
    msg: str
    hint: str | None = None
    id: str | None = None
    obj: object | None = None

    def __str__(self) -> str:
        return self.msg

    @property
    def is_serious(self) -> bool:
        return self.level >= CheckLevel.ERROR

    @classmethod
    def error(
        cls, msg: str, *, hint: str | None = None, id: str | None = None, obj: object | None = None
    ) -> CheckMessage:
        return cls(CheckLevel.ERROR, msg, hint, id, obj)

    @classmethod
    def warning(
        cls, msg: str, *, hint: str | None = None, id: str | None = None, obj: object | None = None
    ) -> CheckMessage:
        return cls(CheckLevel.WARNING, msg, hint, id, obj)

    @classmethod
    def info(
        cls, msg: str, *, hint: str | None = None, id: str | None = None, obj: object | None = None
    ) -> CheckMessage:
        return cls(CheckLevel.INFO, msg, hint, id, obj)

    @classmethod
    def debug(
        cls, msg: str, *, hint: str | None = None, id: str | None = None, obj: object | None = None
    ) -> CheckMessage:
        return cls(CheckLevel.DEBUG, msg, hint, id, obj)


class Error(CheckMessage):
    def __init__(
        self, msg: str, *, hint: str | None = None, id: str | None = None, obj: object | None = None
    ) -> None:
        super().__init__(CheckLevel.ERROR, msg, hint, id, obj)


class Warning(CheckMessage):  # noqa: A001  (mirrors Django's checks API)
    def __init__(
        self, msg: str, *, hint: str | None = None, id: str | None = None, obj: object | None = None
    ) -> None:
        super().__init__(CheckLevel.WARNING, msg, hint, id, obj)


class Info(CheckMessage):
    def __init__(
        self, msg: str, *, hint: str | None = None, id: str | None = None, obj: object | None = None
    ) -> None:
        super().__init__(CheckLevel.INFO, msg, hint, id, obj)
