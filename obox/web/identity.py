"""Native dependency injection for request identities."""

from typing import Any

from fastapi import Depends, Request

from obox.core.principal import ANONYMOUS


async def get_principal(request: Request) -> Any:
    return request.scope.get("user", ANONYMOUS)


async def identify(request: Request, principal: Any = Depends(get_principal)) -> Any:
    request.scope["user"] = principal
    request.state._obox_identified = True
    return principal
