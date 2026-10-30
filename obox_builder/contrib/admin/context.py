"""AdminContext: request-scoped bundle passed to ModelView hooks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.fields import RecordContext

if TYPE_CHECKING:
    from obox_builder.contrib.admin.options import ModelView
    from obox_builder.contrib.admin.site import AdminSite


@dataclass
class AdminContext:
    """Everything an admin hook may need: request, user, session, site, admin."""

    request: Request
    user: Any
    db: AsyncSession
    site: AdminSite
    admin: ModelView | None = None
    obj: Any = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def record_context(self, values: Mapping[str, Any] | None = None) -> RecordContext:
        return RecordContext(
            obj=self.obj,
            values=dict(values or {}),
            user=self.user,
            extra=self.extra,
        )

    def operation_context(self):
        from obox_builder.resources import OperationContext
        from obox_builder.security import Actor

        registry = self.site.resource_registry
        actor = getattr(getattr(self.request, "state", None), "actor", None)
        if actor is None:
            if hasattr(self.user, "actor"):
                actor = self.user.actor()
            else:
                actor = Actor(
                    id=getattr(self.user, "id", None),
                    is_active=getattr(self.user, "is_active", True),
                    is_superuser=getattr(self.user, "is_superuser", False),
                    permissions=frozenset(
                        name for name in registry.permissions if self.user.has_perm(name)
                    ),
                )
        return OperationContext(
            actor=actor,
            db=self.db,
            registry=registry,
            obj=self.obj,
            scopes=self.extra.get("api_scopes"),
            metadata={
                "source": "admin_ui",
                "namespace": self.site.name,
                "request_id": getattr(getattr(self.request, "state", None), "request_id", None),
            },
        )


def csrf_token_for(request: Request) -> str:
    """The session-bound CSRF token (empty when there is no session)."""
    from obox_builder.contrib.auth.csrf import get_csrf_token

    session = getattr(request.state, "auth_session", None)
    return get_csrf_token(session) if session is not None else ""


def current_site(request: Request) -> AdminSite:
    """The AdminSite bound to this router, with the global site as fallback."""
    from obox_builder.contrib.admin.site import site as default_site

    return getattr(request.state, "admin_site", None) or default_site
