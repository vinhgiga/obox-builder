"""Isolated admin server for browser regression tests."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import uvicorn


def main() -> None:
    port = int(os.environ.pop("OBOX_BROWSER_PORT"))
    with tempfile.TemporaryDirectory(prefix="obox-navigation-") as directory:
        os.environ.update(
            OBOX_SETTINGS_MODULE="tests.browser.settings",
            OBOX_DATABASE_URL=f"sqlite+aiosqlite:///{Path(directory) / 'test.sqlite3'}",
            OBOX_DEBUG="true",
        )
        import obox_builder

        obox_builder.setup()
        from obox_builder.contrib.admin import ModelAdmin, TabularInline
        from obox_builder.contrib.admin.site import site
        from obox_builder.contrib.auth.permissions import sync_permissions
        from obox_builder.contrib.auth.services import create_user
        from obox_builder.core.application import create_app
        from obox_builder.db import get_engine
        from obox_builder.db.base import ModelBase
        from obox_builder.db.engine import run_async
        from obox_builder.db.session import session_scope

        from tests.support.apps.commentsdemo.models import Ticket
        from tests.support.apps.shop.models import Label, Order, OrderLine, Product

        class LineInline(TabularInline):
            model = OrderLine
            fields = ("sku", "quantity", "product_id")
            autocomplete_fields = ("product_id",)

        class OrderAdmin(ModelAdmin):
            fields = ("reference", "labels")
            list_display = ("id", "reference", "labels")
            autocomplete_fields = ("labels",)
            inlines = (LineInline,)

        site.register(Label, type("LabelAdmin", (ModelAdmin,), {"search_fields": ("title",)}))
        site.register(OrderLine)
        site.register(Order, OrderAdmin)
        product_admin = site.get_model_admin_for(Product)
        assert product_admin is not None
        type(product_admin).list_editable = ("price_cents",)

        async def seed() -> None:
            async with get_engine().begin() as connection:
                await connection.run_sync(ModelBase.metadata.create_all)
            async with session_scope() as db:
                await sync_permissions(db)
                user = await create_user(db, "browser-user", "Browser-test-password-2026!")
                user.is_superuser = True
                db.add_all([Product(name=f"Product {i}", price_cents=i) for i in range(70)])
                db.add_all(
                    [Label(id=f"label-{i:02}", title=f"Browser label {i:02}") for i in range(25)]
                )
                db.add(Ticket(title="Browser discussion"))

        run_async(seed())
        application = create_app()
        application.include_router(site.get_router(), prefix="/admin")
        uvicorn.run(application, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
