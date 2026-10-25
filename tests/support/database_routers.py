from __future__ import annotations

from typing import Any


class ShopRouter:
    def db_for_read(self, model: type[Any], **hints: Any) -> str | None:
        return "analytics" if model._model_label == "shop" else None

    def db_for_write(self, model: type[Any], **hints: Any) -> str | None:
        return "analytics" if model._model_label == "shop" else None

    def allow_migrate(
        self,
        database: str,
        app_label: str,
        model_name: str | None = None,
        **hints: Any,
    ) -> bool | None:
        return database == "analytics" if app_label == "shop" else None


class SplitShopRouter:
    def db_for_read(self, model: type[Any], **hints: Any) -> str | None:
        if model._model_label != "shop":
            return None
        return "analytics" if model.__name__ == "Product" else "default"

    def db_for_write(self, model: type[Any], **hints: Any) -> str | None:
        return self.db_for_read(model, **hints)


class SplitManyRouter:
    def db_for_read(self, model: type[Any], **hints: Any) -> str | None:
        if model._model_label != "shop":
            return None
        return "analytics" if model.__name__ == "Order" else "default"

    def db_for_write(self, model: type[Any], **hints: Any) -> str | None:
        return self.db_for_read(model, **hints)


class AuthRouter:
    def db_for_read(self, model: type[Any], **hints: Any) -> str | None:
        return "analytics" if model._model_label == "auth" else None

    def db_for_write(self, model: type[Any], **hints: Any) -> str | None:
        return self.db_for_read(model, **hints)

    def allow_migrate(
        self,
        database: str,
        app_label: str,
        model_name: str | None = None,
        **hints: Any,
    ) -> bool | None:
        return database == "analytics" if app_label == "auth" else None


class SplitReadWriteRouter:
    def db_for_read(self, model: type[Any], **hints: Any) -> str | None:
        return "analytics" if model._model_label == "shop" else None

    def db_for_write(self, model: type[Any], **hints: Any) -> str | None:
        return "default" if model._model_label == "shop" else None


class InvalidRouter:
    def db_for_read(self, model: type[Any], **hints: Any) -> str:
        return "missing"


class WarehouseRouter:
    def db_for_read(self, model: type[Any], **hints: Any) -> str | None:
        return "analytics" if model._model_label == "warehouse" else None

    def db_for_write(self, model: type[Any], **hints: Any) -> str | None:
        return self.db_for_read(model, **hints)
