from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from starlette.exceptions import HTTPException

from obox._async import invoke
from obox.core.exceptions import PermissionDenied as DjangoPermissionDenied
from obox.rest_framework import exceptions
from obox.rest_framework.response import Response
from obox.rest_framework.settings import api_settings
from obox.views.generic.base import View, _request_session


class APIView(View):
    authentication_classes: Any = None
    permission_classes: Any = None
    include_in_schema = True

    @classmethod
    def as_view(cls, **initkwargs: Any) -> Any:
        for key in initkwargs:
            if key != "_actions" and (key in cls.http_method_names or not hasattr(cls, key)):
                raise TypeError(f"{cls.__name__}.as_view() received an invalid keyword {key!r}.")

        async def view(request: Request) -> Any:
            self = cls(**initkwargs)
            self.setup(request, **request.path_params)
            try:
                async with _request_session(request):
                    response = await self.dispatch(request, **self.kwargs)
            except exceptions.APIException as exc:
                headers = {}
                status = exc.status_code
                if isinstance(exc, (exceptions.NotAuthenticated, exceptions.AuthenticationFailed)):
                    auth = self.get_authenticators()
                    header = auth[0].authenticate_header(request) if auth else None
                    if header:
                        headers["WWW-Authenticate"] = header
                    else:
                        status = 403
                response = Response(
                    exc.detail if isinstance(exc.detail, (dict, list)) else {"detail": exc.detail},
                    status=status,
                    headers=headers,
                )
            except (HTTPException, DjangoPermissionDenied) as exc:
                response = Response(
                    {"detail": str(exc.detail) if isinstance(exc, HTTPException) else str(exc)},
                    status=exc.status_code if isinstance(exc, HTTPException) else 403,
                )
            response.headers["Allow"] = ", ".join(self._allowed_methods())
            return response

        endpoint: Any = view
        endpoint.view_class = cls
        endpoint.view_initkwargs = initkwargs
        endpoint.http_method_names = cls.http_method_names
        endpoint.include_in_schema = True
        return endpoint

    def get_authenticators(self) -> list[Any]:
        classes = (
            api_settings.DEFAULT_AUTHENTICATION_CLASSES
            if self.authentication_classes is None
            else self.authentication_classes
        )
        return [cls() for cls in classes]

    def get_permissions(self) -> list[Any]:
        classes = (
            api_settings.DEFAULT_PERMISSION_CLASSES
            if self.permission_classes is None
            else self.permission_classes
        )
        return [cls() for cls in classes]

    async def check_permissions(self, request: Request) -> None:
        for permission in self.get_permissions():
            if not await invoke(permission.has_permission, request, self):
                self.permission_denied(request, getattr(permission, "message", None))

    async def check_object_permissions(self, request: Request, obj: Any) -> None:
        for permission in self.get_permissions():
            if not await invoke(permission.has_object_permission, request, self, obj):
                self.permission_denied(request, getattr(permission, "message", None))

    def permission_denied(self, request: Request, message: Any = None) -> Any:
        if self.get_authenticators() and not getattr(self, "_successful_authenticator", None):
            raise exceptions.NotAuthenticated()
        raise exceptions.PermissionDenied(message)

    async def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        accepted = request.headers.get("accept", "*/*")
        if not any(media in accepted for media in ("application/json", "application/*", "*/*")):
            raise exceptions.NotAcceptable()
        self._successful_authenticator = None
        for authenticator in self.get_authenticators():
            result = await invoke(authenticator.authenticate, request)
            if result is not None:
                request.scope["user"], request.state.auth = result
                self._successful_authenticator = authenticator
                break
        await self.check_permissions(request)

    async def dispatch(self, request: Request, *args: Any, **kwargs: Any) -> Any:
        await invoke(self.initial, request, *args, **kwargs)
        return await super().dispatch(request, *args, **kwargs)

    async def _data(self) -> Any:
        body = await self.request.body()
        if not body:
            return {}
        content_type = self.request.headers.get("content-type", "").split(";", 1)[0]
        if content_type != "application/json":
            raise exceptions.UnsupportedMediaType(
                f'Unsupported media type "{content_type}" in request.'
            )

        def invalid(value: str) -> Any:
            raise ValueError(value)

        try:
            return json.loads(body, parse_constant=invalid)
        except (ValueError, UnicodeError) as exc:
            raise exceptions.ParseError("JSON parse error.") from exc

    def http_method_not_allowed(self, request: Request, *args: Any, **kwargs: Any) -> Any:
        raise exceptions.MethodNotAllowed(request.method)

    def options(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return Response(
            {
                "name": type(self).__name__,
                "description": type(self).__doc__ or "",
                "renders": ["application/json"],
                "parses": ["application/json"],
            }
        )
