"""Exceptions shared by the supported Django contracts."""

from collections.abc import Mapping
from typing import Any

NON_FIELD_ERRORS = "__all__"


class ImproperlyConfigured(Exception):
    pass


class AppRegistryNotReady(Exception):
    pass


class PermissionDenied(Exception):
    pass


class ValidationError(Exception):
    message_dict: dict[str, list[str]]
    messages: list[str]

    def __init__(self, message: Any, code: str | None = None, params: Any = None) -> None:
        self.code, self.params = code, params
        if isinstance(message, ValidationError):
            message = message.message_dict if hasattr(message, "message_dict") else message.messages
        if isinstance(message, Mapping):
            self.message_dict = {
                key: ValidationError(value).messages for key, value in message.items()
            }
            self.messages = [value for values in self.message_dict.values() for value in values]
        elif isinstance(message, (list, tuple)):
            self.messages = [text for item in message for text in ValidationError(item).messages]
        else:
            self.messages = [str(message) % params if params else str(message)]
        super().__init__(self.message_dict if hasattr(self, "message_dict") else self.messages)
