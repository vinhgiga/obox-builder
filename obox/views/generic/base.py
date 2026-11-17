"""Per-request class-based views over native Starlette requests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, ClassVar, cast

from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from starlette.exceptions import HTTPException

from obox._async import invoke
from obox.conf import settings
from obox.core.exceptions import ImproperlyConfigured, PermissionDenied
from obox.db import _session


async def _identify(request: Request) -> Any:
    from obox._auth import Principal

    runtime = getattr(request.app.state, "obox", None)
    resolver = runtime.settings.principal_dependency if runtime is not None else None
    if resolver is None:
        actor = request.scope.get("user", Principal())
    else:
        from inspect import signature

        actor = (
            await invoke(resolver, request)
            if signature(resolver).parameters
            else await invoke(resolver)
        )
    request.scope["user"] = actor
    return actor


@asynccontextmanager
async def _request_session(request: Request) -> AsyncIterator[None]:
    actor = await _identify(request)
    if settings.DATABASES:
        async with _session() as session:
            from obox._context import Context

            runtime = getattr(request.app.state, "obox", None)
            session.info["obox._context"] = Context(
                principal=actor,
                session=session,
                request=request,
                source="ui",
                _owner=runtime,
                _active=True,
                _transaction=session.sync_session.get_transaction(),
            )
            yield
    else:
        yield


class View:
    http_method_names: ClassVar[list[str]] = [
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "options",
        "trace",
    ]

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)
        self.request: Request
        self.args: tuple[Any, ...]
        self.kwargs: dict[str, Any]

    @classmethod
    def as_view(cls, **initkwargs: Any) -> Any:
        for key in initkwargs:
            if key in cls.http_method_names or not hasattr(cls, key):
                raise TypeError(f"{cls.__name__}.as_view() received an invalid keyword {key!r}.")

        async def view(request: Request) -> Response:
            self = cls(**initkwargs)
            self.setup(request, **request.path_params)
            try:
                async with _request_session(request):
                    from obox.middleware.csrf import _check, _set_cookie

                    await _check(request)
                    response = cast(Response, await invoke(self.dispatch, request, **self.kwargs))
                    _set_cookie(request, response)
                    return response
            except PermissionDenied:
                return HTMLResponse("Forbidden", status_code=403)
            except HTTPException as exc:
                return HTMLResponse(
                    str(exc.detail), status_code=exc.status_code, headers=exc.headers
                )

        endpoint: Any = view
        endpoint.view_class = cls
        endpoint.view_initkwargs = initkwargs
        endpoint.http_method_names = cls.http_method_names
        return endpoint

    def setup(self, request: Request, *args: Any, **kwargs: Any) -> None:
        self.request, self.args, self.kwargs = request, args, kwargs
        if hasattr(self, "get") and not hasattr(self, "head"):
            self.head = cast(Any, self).get

    async def dispatch(self, request: Request, *args: Any, **kwargs: Any) -> Any:
        method = request.method.lower()
        handler = (
            getattr(self, method, self.http_method_not_allowed)
            if method in self.http_method_names
            else self.http_method_not_allowed
        )
        return await invoke(handler, request, *args, **kwargs)

    def _allowed_methods(self) -> list[str]:
        return [method.upper() for method in self.http_method_names if hasattr(self, method)]

    def http_method_not_allowed(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return Response(status_code=405, headers={"Allow": ", ".join(self._allowed_methods())})

    def options(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return Response(
            headers={"Allow": ", ".join(self._allowed_methods()), "Content-Length": "0"}
        )


class ContextMixin:
    extra_context: dict[str, Any] | None = None

    def get_context_data(self, **kwargs: Any) -> Any:
        return {"view": self, **kwargs, **(self.extra_context or {})}


class TemplateResponseMixin:
    template_name: str | None = None
    content_type: str | None = None
    request: Request

    def get_template_names(self) -> list[str]:
        if self.template_name is None:
            raise ImproperlyConfigured("Define template_name or override get_template_names().")
        return [self.template_name]

    def render_to_response(self, context: dict[str, Any], **response_kwargs: Any) -> Response:
        from fastapi.templating import Jinja2Templates

        from obox.middleware.csrf import get_token

        runtime = getattr(self.request.app.state, "obox", None)
        if runtime is not None:
            templates = runtime.templates
        else:
            from importlib import import_module

            from obox.template.backends.jinja2 import Jinja2

            config = settings.TEMPLATES[0] if settings.TEMPLATES else {}
            backend = Jinja2
            if config.get("BACKEND"):
                module, _, name = config["BACKEND"].rpartition(".")
                backend = getattr(import_module(module), name)
            templates = Jinja2Templates(env=backend(config).env)
        context.setdefault("csrf_token", get_token(self.request))
        names = self.get_template_names()
        template = templates.env.select_template(names)
        return cast(
            Response,
            templates.TemplateResponse(
                request=self.request,
                name=template.name or names[0],
                context={"request": self.request, **context},
                **response_kwargs,
            ),
        )


class TemplateView(TemplateResponseMixin, ContextMixin, View):
    async def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return self.render_to_response(await invoke(self.get_context_data, **kwargs))
