from __future__ import annotations

from functools import lru_cache
from typing import Any

from obox_builder.conf import settings
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.utils import import_string


@lru_cache(maxsize=32)
def _load_routers(paths: tuple[str, ...]) -> tuple[object, ...]:
    return tuple(import_string(path)() for path in paths)


class ConnectionRouter:
    """Resolve model reads, writes, relations, and migrations to database aliases."""

    def __init__(self, routers: list[object | str] | None = None) -> None:
        self._configured_routers = routers

    @property
    def routers(self) -> tuple[object, ...]:
        configured = self._configured_routers
        if configured is None:
            return _load_routers(tuple(settings.DATABASE_ROUTERS))
        return tuple(
            import_string(router)() if isinstance(router, str) else router for router in configured
        )

    @staticmethod
    def _validate_alias(alias: str | None) -> str | None:
        if alias is None:
            return None
        if alias not in settings.database_urls:
            configured = ", ".join(sorted(settings.database_urls))
            raise ImproperlyConfigured(
                f"Database router selected unknown alias {alias!r}. Configured aliases: "
                f"{configured}."
            )
        return alias

    def _route(self, method: str, *args: Any, **hints: Any) -> str:
        for router in self.routers:
            callback = getattr(router, method, None)
            if callback is not None:
                alias = callback(*args, **hints)
                if alias is not None:
                    return self._validate_alias(alias) or "default"
        instance = hints.get("instance")
        if instance is not None:
            alias = getattr(getattr(instance, "_sa_instance_state", None), "info", {}).get(
                "database_alias"
            )
            if alias is not None:
                return self._validate_alias(alias) or "default"
        return "default"

    def db_for_read(self, model: type[Any], **hints: Any) -> str:
        return self._route("db_for_read", model, **hints)

    def db_for_write(self, model: type[Any], **hints: Any) -> str:
        return self._route("db_for_write", model, **hints)

    def allow_relation(self, obj1: object, obj2: object, **hints: Any) -> bool:
        for database_router in self.routers:
            callback = getattr(database_router, "allow_relation", None)
            if callback is not None:
                allowed = callback(obj1, obj2, **hints)
                if allowed is not None:
                    return bool(allowed)
        state1 = getattr(obj1, "_sa_instance_state", None)
        state2 = getattr(obj2, "_sa_instance_state", None)
        alias1 = getattr(state1, "info", {}).get("database_alias")
        alias2 = getattr(state2, "info", {}).get("database_alias")
        if alias1 is None:
            alias1 = self.db_for_write(type(obj1), instance=obj1)
        if alias2 is None:
            alias2 = self.db_for_write(type(obj2), instance=obj2)
        return alias1 == alias2

    def allow_migrate(
        self,
        database: str,
        app_label: str,
        model_name: str | None = None,
        **hints: Any,
    ) -> bool:
        self._validate_alias(database)
        for database_router in self.routers:
            callback = getattr(database_router, "allow_migrate", None)
            if callback is not None:
                allowed = callback(database, app_label, model_name=model_name, **hints)
                if allowed is not None:
                    return bool(allowed)
        return True

    def allow_migrate_model(self, database: str, model: type[Any]) -> bool:
        return self.allow_migrate(
            database,
            model._model_label,
            model_name=model.__name__.lower(),
            model=model,
        )

    def validate(self, database: str | None = None) -> None:
        """Raise when router imports or model routing select an unknown alias."""
        from obox_builder.apps import apps

        aliases = [database] if database is not None else list(settings.database_urls)
        for alias in aliases:
            self._validate_alias(alias)
            self.allow_migrate(alias, "")
        if apps.models_ready:
            for model in apps.get_models():
                self.db_for_read(model)
                self.db_for_write(model)


router = ConnectionRouter()

__all__ = ["ConnectionRouter", "router"]
