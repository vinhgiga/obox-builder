"""The Django Jinja2 backend contract backed by native Jinja environments."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PackageLoader


class Jinja2:
    def __init__(self, params: dict[str, Any]) -> None:
        self.name = params.get("NAME", "jinja2")
        options = {"autoescape": True, **params.get("OPTIONS", {})}
        environment = options.pop("environment", "jinja2.Environment")
        module, _, name = environment.rpartition(".")
        factory = getattr(import_module(module), name)
        directories = list(params.get("DIRS", ()))
        loaders = [
            FileSystemLoader(directories),
            PackageLoader("obox", "templates"),
            PackageLoader("obox.contrib.auth", "templates"),
            PackageLoader("obox.contrib.files", "templates"),
            PackageLoader("obox.contrib.analytics", "templates"),
        ]
        options.setdefault("loader", ChoiceLoader(loaders))
        options.setdefault("extensions", ["jinja2.ext.i18n"])
        self.env: Environment = factory(**options)
        install = getattr(self.env, "install_null_translations", None)
        if install:
            install()
