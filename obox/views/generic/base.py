"""Per-request class-based views over native Starlette requests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, ClassVar, cast

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, Response
from markupsafe import escape
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.types import Message, Receive, Scope, Send

from obox.conf import settings
from obox.core.async_utils import invoke
from obox.core.exceptions import ImproperlyConfigured, OboxError, PermissionDenied
from obox.db.session import current_session, session_scope
from obox.web.identity import identify
from obox.web.routes import MethodRoute, error_status


async def _html_error(request: Request, exc: Exception) -> Response:
    from jinja2 import TemplateNotFound

    runtime = getattr(request.app.state, "obox", None)
    status = 403 if isinstance(exc, PermissionDenied) else error_status(exc)
    if runtime is not None and isinstance(exc, OboxError):
        request.state.obox_html_permission = True
        response = await runtime.problem(request, exc)
    else:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        if isinstance(exc, PermissionDenied) and not detail:
            detail = "Forbidden"
        response = HTMLResponse(
            escape("Invalid input." if isinstance(exc, RequestValidationError) else str(detail)),
            status_code=status,
            headers=exc.headers if isinstance(exc, HTTPException) else None,
        )
    templates = (
        runtime.templates if runtime is not None else getattr(request.app.state, "templates", None)
    )
    if (
        templates is not None
        and status in {403, 404}
        and request.headers.get("x-obox-request") != "true"
    ):
        name = f"{status}.html"
        try:
            templates.env.get_template(name)
        except TemplateNotFound:
            pass
        else:
            return cast(
                Response,
                templates.TemplateResponse(
                    request=request,
                    name=name,
                    status_code=status,
                    context={
                        **getattr(request.state, "obox_template_context", {}),
                        "principal": request.scope.get("user"),
                    },
                ),
            )
    return cast(Response, response)


class HTMLRoute(MethodRoute):
    """Translate failures including function-scoped dependency teardown."""

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = False

        async def respond(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "private, no-store"
                headers.add_vary_header("Cookie, Authorization, Accept-Language")
            elif scope["method"] == "HEAD" and message["type"] == "http.response.body":
                message = {**message, "body": b""}
            await send(message)

        try:
            await super().handle(scope, receive, respond)
        except (OboxError, PermissionDenied, HTTPException, RequestValidationError) as exc:
            if started:
                raise
            response = await _html_error(Request(scope, receive), exc)
            await response(scope, receive, respond)


@asynccontextmanager
async def _request_session(request: Request) -> AsyncIterator[Any]:
    actor = request.scope.get("user")
    runtime = getattr(request.app.state, "obox", None)
    if runtime is not None:
        async with runtime.uow(principal=actor, request=request, source="ui") as context:
            yield context
    elif settings.configured and settings.DATABASES:
        async with session_scope():
            yield None
    else:
        yield None


class View:
    session: Any
    principal: Any
    dependencies: ClassVar[Sequence[Any]] = ()
    principal_dependency: Any = None
    csrf_protect = True
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

    def check(self) -> None:
        """Validate effective configuration before accepting requests."""

    @classmethod
    def as_route(
        cls,
        router: APIRouter,
        path: str,
        *,
        name: str,
        initkwargs: dict[str, Any] | None = None,
        dependencies: Sequence[Any] = (),
    ) -> None:
        options = initkwargs or {}
        endpoint = cls.as_view(**options)
        methods = cls(**options).allowed_methods()
        if "GET" in methods and "HEAD" not in methods:
            methods.append("HEAD")
        router.add_api_route(
            path,
            endpoint,
            name=name,
            methods=methods,
            dependencies=[*cls.dependencies, *dependencies],
            response_model=None,
            include_in_schema=False,
            route_class_override=HTMLRoute,
        )

    def reverse(self, name: str, **params: Any) -> str:
        path = str(self.request.app.url_path_for(name, **params))
        return str(self.request.scope.get("root_path", "")).rstrip("/") + path

    def redirect(self, name_or_path: str, **params: Any) -> Response:
        from starlette.responses import RedirectResponse

        return RedirectResponse(
            name_or_path if name_or_path.startswith("/") else self.reverse(name_or_path, **params),
            status_code=303,
        )

    @classmethod
    def as_view(cls, **initkwargs: Any) -> Any:
        for key in initkwargs:
            if key in cls.http_method_names or not hasattr(cls, key):
                raise TypeError(f"{cls.__name__}.as_view() received an invalid keyword {key!r}.")

        cls(**initkwargs).check()
        principal_dependency = cls.principal_dependency or identify

        async def view(
            request: Request, principal: Any = Depends(principal_dependency)
        ) -> Response:
            request.scope["user"] = principal
            request.state._obox_identified = True
            self = cls(**initkwargs)
            self.setup(request, **request.path_params)
            try:
                async with _request_session(request) as context:
                    current = current_session.get()
                    self.session = (
                        context.session if context is not None else current[1] if current else None
                    )
                    self.principal = request.user
                    runtime = getattr(request.app.state, "obox", None)
                    if runtime is not None and context is not None:
                        from obox.web.i18n import context as translation_context
                        from obox.web.ui import build_navigation

                        request.state.obox_context = context
                        request.state.obox_template_context = {
                            **translation_context(request),
                            "site_title": runtime.app.title,
                            "base_template": runtime.settings.base_template,
                            "navigation": await build_navigation(runtime, request, context),
                            "principal": context.principal,
                            "auth": runtime.auth,
                            "csrf": runtime.csrf.token(request, context.principal),
                        }
                    from obox.middleware.csrf import check_csrf, set_csrf_cookie

                    if self.csrf_protect and not getattr(request.state, "_obox_html_csrf", False):
                        await check_csrf(request, multipart=True)
                    response = cast(Response, await invoke(self.dispatch, request, **self.kwargs))
                    set_csrf_cookie(request, response)
                    return response
            except (PermissionDenied, OboxError, HTTPException, RequestValidationError) as exc:
                return await _html_error(request, exc)
            finally:
                await request.close()

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

    def allowed_methods(self) -> list[str]:
        return [method.upper() for method in self.http_method_names if hasattr(self, method)]

    def http_method_not_allowed(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return Response(status_code=405, headers={"Allow": ", ".join(self.allowed_methods())})

    def options(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return Response(headers={"Allow": ", ".join(self.allowed_methods()), "Content-Length": "0"})


class ContextMixin:
    def check(self) -> None:
        """Cooperate with other view configuration checks."""

    extra_context: dict[str, Any] | None = None

    def get_context_data(self, **kwargs: Any) -> Any:
        return {
            "view": self,
            "principal": getattr(self, "principal", None),
            **kwargs,
            **(self.extra_context or {}),
        }


class TemplateResponseMixin:
    template_name: str | None = None
    content_type: str | None = None
    request: Request

    def check(self) -> None:
        check = getattr(super(), "check", None)
        if check is not None:
            check()
        if (
            type(self).get_template_names is TemplateResponseMixin.get_template_names
            and not self.template_name
        ):
            raise ImproperlyConfigured("Define template_name or override get_template_names().")

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
        elif hasattr(self.request.app.state, "templates"):
            templates = self.request.app.state.templates
        else:
            from importlib import import_module

            from obox.template.backends.jinja2 import Jinja2

            config = settings.TEMPLATES[0] if settings.TEMPLATES else {}
            backend = Jinja2
            if config.get("BACKEND"):
                module, _, name = config["BACKEND"].rpartition(".")
                backend = getattr(import_module(module), name)
            templates = Jinja2Templates(env=backend(config).env)
            self.request.app.state.templates = templates
        if runtime is not None:
            try:
                context.setdefault("csrf_token", get_token(self.request))
            except ImproperlyConfigured:
                context.setdefault(
                    "csrf_token",
                    runtime.csrf.token(self.request, self.request.scope.get("user")),
                )
        else:
            context.setdefault("csrf_token", get_token(self.request))
        context = {
            **getattr(self.request.state, "obox_template_context", {}),
            **context,
        }
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
