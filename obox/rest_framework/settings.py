from importlib import import_module
from typing import Any

from obox.conf import settings

_DEFAULTS = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "obox.rest_framework.authentication.SessionAuthentication",
        "obox.rest_framework.authentication.BasicAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["obox.rest_framework.permissions.AllowAny"],
    "DEFAULT_PAGINATION_CLASS": None,
    "DEFAULT_FILTER_BACKENDS": [],
    "PAGE_SIZE": None,
    "SEARCH_PARAM": "search",
    "ORDERING_PARAM": "ordering",
    "NON_FIELD_ERRORS_KEY": "non_field_errors",
}


def _import(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    module, _, name = value.rpartition(".")
    return getattr(import_module(module), name)


class APISettings:
    def __getattr__(self, name: str) -> Any:
        if name not in _DEFAULTS:
            raise AttributeError(name)
        value = settings.REST_FRAMEWORK.get(name, _DEFAULTS[name])
        if name.endswith("_CLASSES") or name == "DEFAULT_FILTER_BACKENDS":
            return [_import(item) for item in value]
        return _import(value) if name == "DEFAULT_PAGINATION_CLASS" else value


api_settings = APISettings()
