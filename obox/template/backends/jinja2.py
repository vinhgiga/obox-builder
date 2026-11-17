"""The Django Jinja2 backend contract backed by native Jinja environments."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PackageLoader, TemplateNotFound
from jinja2 import TemplateSyntaxError as JinjaSyntaxError

from obox.apps import apps
from obox.template import TemplateDoesNotExist, TemplateSyntaxError


class Template:
    def __init__(self, template: Any, backend: Jinja2) -> None:
        self.template, self.backend = template, backend

    def render(self, context: dict[str, Any] | None = None, request: Any = None) -> str:
        values = dict(context or {})
        if request is not None:
            from obox.middleware.csrf import get_token

            values.update(request=request, csrf_token=get_token(request))
        return str(self.template.render(values))


class Jinja2:
    def __init__(self, params: dict[str, Any]) -> None:
        self.name = params.get("NAME", "jinja2")
        options = {"autoescape": True, **params.get("OPTIONS", {})}
        environment = options.pop("environment", "jinja2.Environment")
        module, _, name = environment.rpartition(".")
        factory = getattr(import_module(module), name)
        directories = list(params.get("DIRS", ()))
        if params.get("APP_DIRS", False):
            directories.extend(
                str(Path(app.path) / "templates") for app in apps.app_configs.values()
            )
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

    def get_template(self, template_name: str) -> Template:
        try:
            return Template(self.env.get_template(template_name), self)
        except TemplateNotFound as exc:
            raise TemplateDoesNotExist(template_name) from exc
        except JinjaSyntaxError as exc:
            raise TemplateSyntaxError(str(exc)) from exc

    def from_string(self, template_code: str) -> Template:
        try:
            return Template(self.env.from_string(template_code), self)
        except JinjaSyntaxError as exc:
            raise TemplateSyntaxError(str(exc)) from exc
