"""Opt-in, transaction-owned change notifications and standalone delivery workers."""

from __future__ import annotations

import base64
import hmac
from collections.abc import Callable, Mapping
from datetime import timedelta
from types import MappingProxyType
from typing import Any

from obox._context import Context
from obox.core.exceptions import ImproperlyConfigured


class Webhooks:
    name = "webhooks"
    requires: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        signing_keys: Mapping[str, bytes],
        active_key: str,
        resolve_principal: Callable[[str, Context], Any],
        allowed_hosts: tuple[str, ...] = (),
        subscription_ttl: timedelta = timedelta(days=7),
        retention: timedelta = timedelta(days=7),
        max_subscriptions: int = 10,
        max_attempts: int = 8,
        models: tuple[type[Any], ...] = (),
    ) -> None:
        if (
            not signing_keys
            or active_key not in signing_keys
            or any(
                not isinstance(key, str)
                or not key
                or len(key) > 255
                or not isinstance(value, bytes)
                or len(value) < 32
                for key, value in signing_keys.items()
            )
        ):
            raise ImproperlyConfigured("Supply named webhook signing keys of at least 32 bytes.")
        if not callable(resolve_principal):
            raise ImproperlyConfigured("Webhooks require a current identity resolver.")
        if subscription_ttl <= timedelta(0) or retention <= timedelta(0):
            raise ImproperlyConfigured("Webhook expiry and retention must be positive.")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (max_subscriptions, max_attempts)
        ):
            raise ImproperlyConfigured("Webhook limits must be positive integers.")
        self.signing_keys = MappingProxyType(dict(signing_keys))
        self.active_key, self.resolve_principal = active_key, resolve_principal
        self.allowed_hosts = tuple(host.lower().encode("idna").decode() for host in allowed_hosts)
        self.subscription_ttl, self.retention = subscription_ttl, retention
        self.max_subscriptions, self.max_attempts = max_subscriptions, max_attempts
        self.models = models

    def _attach(self, app: Any) -> None:
        from obox.contrib.webhooks.routes import build_router

        if hasattr(self, "app"):
            raise ImproperlyConfigured("Webhooks is already installed.")
        self.app = app
        if not self.models:
            from obox.contrib._models import installed_models

            self.models = installed_models()
        app.include_router(build_router(self), permission="webhooks.manage_subscriptions")

    def secret(self, subscription: Any) -> bytes:
        key = self.signing_keys.get(subscription.key_id)
        if key is None:
            raise ImproperlyConfigured("The webhook signing key is unavailable.")
        return hmac.digest(
            key, f"obox.webhook.v1:{subscription.id}:{subscription.nonce}".encode(), "sha256"
        )

    def encoded_secret(self, subscription: Any) -> str:
        return "whsec_" + base64.b64encode(self.secret(subscription)).decode()

    def get_queryset(self, model: type[Any], ctx: Context) -> Any:
        import sqlalchemy as sa

        return sa.select(model)

    def has_permission(self, model: type[Any], ctx: Context, obj: Any = None) -> bool:
        from obox.contrib._models import permission

        return permission(model, ctx.principal, "view")

    async def record(self, action: str, obj: Any, ctx: Context) -> None:
        from obox.contrib.webhooks.services import record

        await record(self, action, obj, ctx)

    async def prune(self, ctx: Context) -> int:
        from obox.contrib.webhooks.services import prune

        return await prune(self, ctx)
