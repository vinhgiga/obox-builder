from __future__ import annotations

import base64
import binascii
from typing import Any

from obox._async import invoke
from obox.rest_framework.exceptions import AuthenticationFailed, PermissionDenied


class BaseAuthentication:
    async def authenticate(self, request: Any) -> Any:
        raise NotImplementedError

    def authenticate_header(self, request: Any) -> str | None:
        return None


class SessionAuthentication(BaseAuthentication):
    async def authenticate(self, request: Any) -> Any:
        user = request.user
        if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", True):
            return None
        from obox.core.exceptions import PermissionDenied as CsrfDenied
        from obox.middleware.csrf import _check

        try:
            await _check(request)
        except CsrfDenied as exc:
            raise PermissionDenied("CSRF Failed: " + str(exc)) from exc
        return user, None


class BasicAuthentication(BaseAuthentication):
    async def authenticate(self, request: Any) -> Any:
        header = request.headers.get("authorization", "").split()
        if not header or header[0].lower() != "basic":
            return None
        try:
            if len(header) != 2:
                raise ValueError
            username, password = base64.b64decode(header[1], validate=True).decode().split(":", 1)
        except (ValueError, UnicodeError, binascii.Error) as exc:
            raise AuthenticationFailed("Invalid basic header.") from exc
        runtime = getattr(request.app.state, "obox", None)
        backend = getattr(getattr(runtime, "auth", None), "backend", None)
        if backend is None or not callable(getattr(backend, "authenticate", None)):
            raise AuthenticationFailed()
        async with backend.session_factory() as session:
            user = await invoke(backend.authenticate, session, username, password, request=request)
            if user is None or not getattr(user, "is_active", True):
                raise AuthenticationFailed()
            user = await invoke(
                backend.get_user, session, backend.get_user_id(user), request=request
            )
            principal = await invoke(backend.to_principal, session, user, request=request)
        return principal, None

    def authenticate_header(self, request: Any) -> str:
        return 'Basic realm="api"'
