"""Jinja2 environment for obox-builder (autoescape on, registry-aware loader).

``render(request, name, context)`` is the entry point used by views and
``obox_builder.http`` shortcuts. The environment is cached and rebuilt whenever
the template search path or the configured context processors change.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import Request
from jinja2 import Environment
from starlette.responses import HTMLResponse

from obox_builder.conf import settings
from obox_builder.contrib.i18n.translations import gettext, ngettext, npgettext, pgettext
from obox_builder.templating.filters import filesizeformat, format_datetime, querystring
from obox_builder.templating.loaders import AppDirectoriesLoader, collect_template_dirs
from obox_builder.utils import import_string

_FRAMEWORK_PROCESSORS = ["obox_builder.templating.context_processors.request"]
_AUTH_PROCESSORS = ["obox_builder.contrib.auth.context_processors.auth"]

_environments: dict[tuple[Any, ...], Environment] = {}


def _context_processor_paths() -> list[str]:
    from obox_builder.apps import apps

    processors = list(_FRAMEWORK_PROCESSORS)
    if apps.is_installed("obox_builder.contrib.auth"):
        processors.extend(_AUTH_PROCESSORS)
    processors.extend(settings.TEMPLATE_CONTEXT_PROCESSORS)
    return processors


def get_environment() -> Environment:
    """Build (or reuse) the Jinja2 environment for the current settings/apps."""
    search_paths = collect_template_dirs()
    processor_paths = _context_processor_paths()
    key = (tuple(str(path) for path in search_paths), tuple(processor_paths))
    environment = _environments.get(key)
    if environment is None:
        environment = Environment(
            loader=AppDirectoriesLoader(search_paths),
            autoescape=True,
            auto_reload=settings.DEBUG,
            extensions=["jinja2.ext.i18n"],
        )
        cast(Any, environment).install_gettext_callables(
            gettext, ngettext, newstyle=True, pgettext=pgettext, npgettext=npgettext
        )
        environment.filters["format_datetime"] = format_datetime
        environment.filters["gettext"] = gettext
        environment.filters["filesizeformat"] = filesizeformat
        environment.globals["querystring"] = querystring

        def static(path: str) -> str:
            from obox_builder.staticfiles import static as static_url

            return static_url(path)

        environment.globals["static"] = static
        _environments[key] = environment
    return environment


def build_context(request: Request, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge context processors with explicit context (explicit context wins)."""
    merged: dict[str, Any] = {}
    for path in _context_processor_paths():
        processor = import_string(path)
        merged.update(processor(request))
    if context:
        merged.update(context)
    return merged


def render_template(name: str, context: dict[str, Any]) -> str:
    """Render a template by name with a fully prepared context."""
    return get_environment().get_template(name).render(context)


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render a template to an ``HTMLResponse`` for a request."""
    return HTMLResponse(
        render_template(name, build_context(request, context)), status_code=status_code
    )


__all__ = [
    "AppDirectoriesLoader",
    "build_context",
    "collect_template_dirs",
    "get_environment",
    "render",
    "render_template",
]
